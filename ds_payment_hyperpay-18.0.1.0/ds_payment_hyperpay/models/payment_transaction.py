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

    # ------------------------------
    # Helpers
    # ------------------------------
    @staticmethod
    def _split_name(full_name):
        """Return (givenName, surname) with simple, safe splitting."""
        if not full_name:
            return ("", "")
        parts = full_name.strip().split()
        if len(parts) == 1:
            return (parts[0], "")
        return (" ".join(parts[:-1]), parts[-1])

    def _billing_payload_from_partner(self, partner, test_mode=False):
        """Build billing/customer payload. In test_mode, fill safe defaults if missing."""
        given_name, surname = self._split_name(partner.name or "")

        email = (partner.email or "").strip()
        street = (partner.street or "").strip()
        city = (partner.city or "").strip()
        postcode = (partner.zip or "").strip()
        country_code = (partner.country_id and partner.country_id.code) or ""
        state_value = ""
        if partner.state_id:
            state_value = partner.state_id.code or partner.state_id.name or ""

        # In TEST MODE: supply safe defaults to avoid format errors
        if test_mode:
            if not email:
                email = "test@example.com"
            if not given_name:
                given_name = "Test"
            if not surname:
                surname = "User"
            if not street:
                street = "Test Street 1"
            if not city:
                city = "Riyadh"
            if not postcode:
                postcode = "11564"
            if not country_code:
                country_code = "SA"

        # In PROD: enforce required fields
        missing = []
        if not email:
            missing.append("customer.email")
        if not street:
            missing.append("billing.street1")
        if not city:
            missing.append("billing.city")
        if not postcode:
            missing.append("billing.postcode")
        if not country_code:
            missing.append("billing.country")

        if missing and not test_mode:
            raise ValidationError(_("Missing required billing fields: %s") % ", ".join(missing))

        return {
            'customer.email': email,
            'customer.givenName': given_name,
            'customer.surname': surname,
            'billing.street1': street,
            'billing.city': city,
            'billing.state': state_value,
            'billing.country': country_code,  # ISO Alpha-2
            'billing.postcode': postcode,
        }

    # ------------------------------
    # Overrides
    # ------------------------------
    @api.model
    def _compute_reference(self, provider_code, prefix=None, separator='-', **kwargs):
        if provider_code == 'hyperpay':
            prefix = payment_utils.singularize_reference_prefix()
        return super()._compute_reference(provider_code, prefix=prefix, separator=separator, **kwargs)

    def _get_specific_rendering_values(self, processing_values):
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'hyperpay':
            return res
        if self.currency_id.id not in self.payment_method_id.supported_currency_ids.ids:
            raise odoo.exceptions.UserError("This currency is not supported with selected payment method.")
        return self.hyperpay_execute_payment()

    # ------------------------------
    # HyperPay: Create checkout + pass customer/billing data
    # ------------------------------
    def hyperpay_execute_payment(self):
        hyperpay_provider = self.provider_id
        payment_method_code = self.payment_method_id.code

        # Choose entityId by method (MADA vs. Cards)
        if payment_method_code == 'mada':
            entity_id = hyperpay_provider.hyperpay_merchant_id_mada
        else:
            entity_id = hyperpay_provider.hyperpay_merchant_id
        if not entity_id:
            raise ValidationError("No entityID provided for '%s' transactions." % payment_method_code)

        partner = self.partner_id.commercial_partner_id
        test_mode = (hyperpay_provider.state != 'enabled')

        # Build billing/customer payload (with safe defaults in test)
        billing_payload = self._billing_payload_from_partner(partner, test_mode=test_mode)

        request_values = {
            'entityId': entity_id,
            'amount': "{:.2f}".format(self.amount),
            'currency': self.currency_id.name,
            'paymentType': 'DB',
            'merchantTransactionId': self.reference,
            **billing_payload,
        }

        if test_mode:
            request_values['testMode'] = 'EXTERNAL'
            request_values['customParameters[3DS2_enrolled]'] = 'true'

        # Create checkout
        response_content = self.provider_id._hyperpay_make_request(request_values)

        # Prepare rendering values
        response_content['action_url'] = '/payment/hyperpay'
        response_content['checkout_id'] = response_content.get('id')
        response_content['merchantTransactionId'] = self.reference
        response_content['formatted_amount'] = format_amount(self.env, self.amount, self.currency_id)
        response_content['paymentMethodCode'] = payment_method_code

        if hyperpay_provider.state == 'enabled':
            payment_url = "https://eu-prod.oppwa.com/v1/paymentWidgets.js?checkoutId=%s" % response_content['checkout_id']
        else:
            payment_url = "https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=%s" % response_content['checkout_id']
        response_content['payment_url'] = payment_url
        return response_content

    # ------------------------------
    # Notifications
    # ------------------------------
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
