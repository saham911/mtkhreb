# -*- coding: utf-8 -*-
#############################################################################
#
#    Copyright (C) 2024-TODAY
#    Author: Odoo DevSouls <odoodevsouls@gmailcom>
#
#############################################################################

import re
import logging
from werkzeug import urls

import odoo.exceptions
from odoo import api, models, _
from odoo.tools import format_amount
from odoo.exceptions import ValidationError
from odoo.addons.payment import utils as payment_utils
from odoo.addons.ds_payment_hyperpay import hyperpay_utils as hyperpay

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # Reference format per provider
    @api.model
    def _compute_reference(self, provider_code, prefix=None, separator='-', **kwargs):
        if provider_code == 'hyperpay':
            prefix = payment_utils.singularize_reference_prefix()
        return super()._compute_reference(provider_code, prefix=prefix, separator=separator, **kwargs)

    # Entry-point from payment flow
    def _get_specific_rendering_values(self, processing_values):
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'hyperpay':
            return res
        if self.currency_id.id not in self.payment_method_id.supported_currency_ids.ids:
            raise odoo.exceptions.UserError("This currency is not supported with selected payment method.")
        return self.hyperpay_execute_payment()

    # =====================  HyperPay: execute payment  ===================== #
    def hyperpay_execute_payment(self):
        """Builds HyperPay request from Odoo partner fields (standard or Studio)."""
        provider = self.provider_id
        pm_code = self.payment_method_id.code

        # entityId selection
        entity_id = provider.hyperpay_merchant_id_mada if pm_code == 'mada' else provider.hyperpay_merchant_id
        if not entity_id:
            raise ValidationError("No entityID provided for '%s' transactions." % pm_code)

        partner = self.partner_id  # payer

        # Helpers
        def _clean(v):
            return (v or "").strip()

        def _split_name(fullname):
            fullname = _clean(fullname)
            if not fullname:
                return "", ""
            parts = fullname.split()
            return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (parts[0], "")

        # Names (prefer Studio fields, else split)
        given = _clean(getattr(partner, 'x_customer_givenname', '') or getattr(partner, 'firstname', ''))
        surname = _clean(getattr(partner, 'x_customer_surname', '') or getattr(partner, 'lastname', ''))
        if not given or not surname:
            s_given, s_surname = _split_name(partner.name or '')
            given = given or s_given
            surname = surname or s_surname

        # Address (prefer Studio billing fields, else standard)
        street1 = _clean(getattr(partner, 'x_billing_street1', '') or partner.street)
        city = _clean(getattr(partner, 'x_billing_city', '') or partner.city)
        postcode = _clean(getattr(partner, 'x_billing_postcode', '') or partner.zip)

        # state: code -> name fallback
        state_val = getattr(partner, 'x_billing_state', '') or (
            partner.state_id and (partner.state_id.code or partner.state_id.name)
        ) or ''
        state_val = _clean(state_val)

        # country: ISO Alpha-2; default SA
        country_code = (partner.country_id and (partner.country_id.code or '')) or ''
        country_code = (country_code or 'SA').upper()[:2]

        # Minimal validation
        missing = []
        if not _clean(partner.email): missing.append("Email")
        if not given:                 missing.append("Given Name")
        if not surname:               missing.append("Surname")
        if not street1:               missing.append("Street")
        if not city:                  missing.append("City")
        if not postcode:              missing.append("Postcode")
        if not country_code:          missing.append("Country (alpha-2)")
        if missing:
            raise odoo.exceptions.UserError(
                "Please complete customer fields before payment: " + ", ".join(missing)
            )

        # Build request (HyperPay format)
        request_values = {
            'entityId': '%s' % entity_id,
            'amount': "{:.2f}".format(self.amount),
            'currency': self.currency_id.name,
            'paymentType': 'DB',
            'merchantTransactionId': self.reference,

            'customer.email': _clean(partner.email),
            'customer.givenName': given,
            'customer.surname': surname,

            'billing.street1': street1,
            'billing.city': city,
            'billing.state': state_val,
            'billing.country': country_code,  # SA for KSA by default
            'billing.postcode': postcode,
        }

        # Test-only flags
        if provider.state != 'enabled':  # test mode
            request_values['testMode'] = 'EXTERNAL'
            request_values['customParameters[3DS2_enrolled]'] = 'true'

        # Send request
        response_content = provider._hyperpay_make_request(request_values)

        # Prepare rendering values
        checkout_id = response_content.get('id')
        response_content.update({
            'action_url': '/payment/hyperpay',
            'checkout_id': checkout_id,
            'merchantTransactionId': response_content.get('merchantTransactionId'),
            'formatted_amount': format_amount(self.env, self.amount, self.currency_id),
            'paymentMethodCode': pm_code,
            'payment_url': (
                "https://eu-prod.oppwa.com/v1/paymentWidgets.js?checkoutId=%s"
                if provider.state == 'enabled'
                else "https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=%s"
            ) % checkout_id
        })
        return response_content
    # ==================  /HyperPay: execute payment  ================== #

    # Notification entry-point
    def _get_tx_from_notification_data(self, provider_code, data):
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code not in ('hyperpay', 'mada'):
            return tx
        payment_status_url = self.provider_id.get_hyperpay_urls()['hyperpay_process_url'] + data.get('resourcePath')
        provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
        notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)
        reference = notification_data.get('merchantTransactionId', False)
        if not reference:
            raise ValidationError(_("HyperPay: No reference found."))
        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')])
        if not tx:
            raise ValidationError(_("HyperPay: No transaction found matching reference %s.") % reference)
        tx._handle_hyperpay_payment_status(notification_data)
        return tx

    # Map status -> Odoo transaction state
    def _handle_hyperpay_payment_status(self, notification_data):
        tx_status_set = False
        status = notification_data.get('result', False)
        if 'id' in notification_data:
            self.provider_reference = notification_data.get('id', False)

        if status and 'code' in status:
            status_code = status.get('code')
            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS']:
                    if re.search(reg_exp, status_code):
                        self._set_done(state_message=status.get('description', "Authorised"))
                        tx_status_set = True
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS_REVIEW']:
                    if re.search(reg_exp, status_code):
                        self._set_pending(state_message=status.get('description'))
                        tx_status_set = True
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['PENDING']:
                    if re.search(reg_exp, status_code):
                        self._set_error(state_message=status.get('description'))
                        tx_status_set = True
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['WAITING']:
                    if re.search(reg_exp, status_code):
                        self._set_error(state_message=status.get('description'))
                        tx_status_set = True
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['REJECTED']:
                    if re.search(reg_exp, status_code):
                        self._set_error(state_message=status.get('description'))
                        tx_status_set = True
                        break

            if not tx_status_set:
                _logger.warning(
                    "Received unrecognized payment state %s for transaction with reference %s\nDetailed Message:%s",
                    status_code, self.reference, status.get('description')
                )
                self._set_error("HyperPay: " + _("Invalid payment status."))
