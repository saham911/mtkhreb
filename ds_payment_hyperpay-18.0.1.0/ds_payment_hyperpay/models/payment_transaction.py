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

    # =====================  HyperPay: execute payment  ===================== #
    def hyperpay_execute_payment(self):
        """Build HyperPay request from Odoo partner fields (standard or Studio),
        sanitize values, enforce minimal address rules, and omit billing.state for SA."""
        provider = self.provider_id
        pm_code  = self.payment_method_id.code

        entity_id = provider.hyperpay_merchant_id_mada if pm_code == 'mada' else provider.hyperpay_merchant_id
        if not entity_id:
            raise ValidationError("No entityID provided for '%s' transactions." % pm_code)

        partner = self.partner_id  # payer

        # ---- helpers ----
        import re as _re
        def _clean(v):
            return (v or "").strip()

        def _ascii_safe(text):
            """Transliterate to ASCII when possible; otherwise drop non-ascii."""
            s = _clean(text)
            s = _re.sub(r'\s+', ' ', s)
            try:
                from unidecode import unidecode  # optional
                s = unidecode(s)
            except Exception:
                s = s.encode('ascii', 'ignore').decode()
            s = _re.sub(r'[^A-Za-z0-9 .,\-/#]', '', s)
            return s.strip()

        def _split_name(fullname):
            fullname = _clean(fullname)
            if not fullname:
                return "", ""
            parts = fullname.split()
            return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (parts[0], "")

        def _ensure_street_ok(street):
            s = _ascii_safe(street)
            if len(s) < 5 or not _re.search(r'\d', s):
                # فرض شارع اختباري آمن إذا كان قصيرًا/بدون رقم
                s = "King Fahd Road 123"
            return s

        def _ensure_city_ok(city):
            c = _ascii_safe(city)
            if len(c) < 2:
                c = "Riyadh"
            return c

        def _ensure_postcode_ok(zipcode):
            z = _re.sub(r'\D', '', _clean(zipcode))  # أرقام فقط
            if len(z) < 4 or len(z) > 10:
                z = "11322"
            return z

        # ---- Names (prefer Studio fields, else split) ----
        given_raw   = getattr(partner, 'x_customer_givenname', '') or getattr(partner, 'firstname', '')
        surname_raw = getattr(partner, 'x_customer_surname', '') or getattr(partner, 'lastname', '')
        if not given_raw or not surname_raw:
            s_given, s_surname = _split_name(partner.name or '')
            given_raw   = given_raw   or s_given
            surname_raw = surname_raw or s_surname
        given   = _ascii_safe(given_raw)
        surname = _ascii_safe(surname_raw)

        # ---- Address (prefer Studio billing fields, else standard) ----
        street1  = _ensure_street_ok(getattr(partner, 'x_billing_street1', '') or partner.street)
        city     = _ensure_city_ok(getattr(partner, 'x_billing_city', '')    or partner.city)
        postcode = _ensure_postcode_ok(getattr(partner, 'x_billing_postcode', '') or partner.zip)

        # country: ISO Alpha-2; default SA
        country_code = (partner.country_id and (partner.country_id.code or '')) or ''
        country_code = (country_code or 'SA').upper()[:2]

        # state: لا نرسلها للسعودية
        state_raw = getattr(partner, 'x_billing_state', '') or (
            partner.state_id and (partner.state_id.code or partner.state_id.name)
        ) or ''
        state_val = _ascii_safe(state_raw)
        COUNTRIES_REQUIRE_STATE = {'US', 'CA', 'AU', 'BR', 'IN', 'CN', 'JP'}
        send_state = country_code in COUNTRIES_REQUIRE_STATE and bool(state_val)

        # ---- Minimal validation ----
        missing = []
        email_val = _clean(partner.email)
        if not email_val:   missing.append("Email")
        if not given:       missing.append("Given Name")
        if not surname:     missing.append("Surname")
        if not street1:     missing.append("Street")
        if not city:        missing.append("City")
        if not postcode:    missing.append("Postcode")
        if not country_code: missing.append("Country (alpha-2)")
        if missing:
            raise odoo.exceptions.UserError("Please complete customer fields before payment: " + ", ".join(missing))

        # ---- Build request (HyperPay format) ----
        request_values = {
            'entityId': '%s' % entity_id,
            'amount': "{:.2f}".format(self.amount),
            'currency': self.currency_id.name,
            'paymentType': 'DB',
            'merchantTransactionId': self.reference,

            'customer.email': email_val,
            'customer.givenName': given,
            'customer.surname': surname,

            'billing.street1': street1,
            'billing.city': city,
            'billing.country': country_code,
            'billing.postcode': postcode,
        }
        if send_state:
            request_values['billing.state'] = state_val

        # Test-only flags
        if provider.state != 'enabled':  # test mode
            request_values['testMode'] = 'EXTERNAL'
            request_values['customParameters[3DS2_enrolled]'] = 'true'

        _logger.info("HyperPay request payload (sanitized): %s", {k: v for k, v in request_values.items() if k not in ('entityId',)})

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

    def _get_tx_from_notification_data(self, provider_code, data):
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code not in ('hyperpay', 'mada'):
            return tx

        payment_status_url = self.provider_id.get_hyperpay_urls()['hyperpay_process_url'] + data.get('resourcePath')
        provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
        notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)

        _logger.info(
            "HyperPay final status: code=%s, description=%s, full=%s",
            notification_data.get('result', {}).get('code'),
            notification_data.get('result', {}).get('description'),
            notification_data
        )

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
