# -*- coding: utf-8 -*-
#############################################################################
#
#    Copyright (C) 2024
#    Author: Odoo DevSouls <odoodevsouls@gmailcom>
#
#############################################################################

import re
import json
import logging
from werkzeug import urls

import odoo.exceptions
from odoo import api, models, _
from odoo.tools import format_amount
from odoo.exceptions import ValidationError, UserError
from odoo.addons.payment import utils as payment_utils
from odoo.addons.ds_payment_hyperpay import hyperpay_utils as hyperpay

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # ------------------------------
    # Helper utilities (logging + safe masking)
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

    @staticmethod
    def _masked(val, keep=4):
        """Mask a string except last <keep> chars."""
        if not val:
            return val
        s = str(val)
        if len(s) <= keep:
            return "*" * len(s)
        return ("*" * (len(s) - keep)) + s[-keep:]

    @classmethod
    def _safe_dump(cls, data):
        """Dump dict to JSON with masking for sensitive keys."""
        if not isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False, default=str)
        safe = {}
        for k, v in data.items():
            key = str(k).lower()
            if key in ('entityid', 'access_token', 'accesstoken', 'authorization', 'merchant_id', 'merchanttransactionid'):
                safe[k] = cls._masked(v, keep=4)
            else:
                safe[k] = v
        return json.dumps(safe, ensure_ascii=False, default=str)

    def _log(self, level, msg, **kv):
        payload = ""
        if kv:
           try:
              payload = " | " + self._safe_dump(kv)
           except Exception:
              payload = " | <kv-dump-error>"
        line = f"[HyperPay] (tx:{self.reference or 'N/A'}) {msg}{payload}"
        # إجبار الإخراج كـ INFO على Odoo.sh
        _logger.info(line)

    # ------------------------------
    # Build billing/customer payload
    # ------------------------------
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

        if test_mode:
            # Fill defaults in test mode to avoid format errors
            email = email or "test@example.com"
            given_name = given_name or "Test"
            surname = surname or "User"
            street = street or "Test Street 1"
            city = city or "Riyadh"
            postcode = postcode or "11564"
            country_code = country_code or "SA"

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

        payload = {
            'customer.email': email,
            'customer.givenName': given_name,
            'customer.surname': surname,
            'billing.street1': street,
            'billing.city': city,
            #'billing.state': state_value,
            'billing.country': country_code,  # ISO Alpha-2
            'billing.postcode': postcode,
        }
        states_required = {'US', 'CA'}
        if country_code in states_required and state_value:
            payload['billing.state'] = state_value
        self._log('debug', "Built billing payload", billing_payload=payload, test_mode=test_mode)
        return payload

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
            raise UserError(_("This currency is not supported with selected payment method."))

        self._log('info', "Start hyperpay_execute_payment (render)")
        try:
            result = self.hyperpay_execute_payment()
            self._log('info', "hyperpay_execute_payment OK", response_keys=list(result.keys()))
            return result
        except Exception as e:
            self._log('error', f"hyperpay_execute_payment FAILED: {e}")
            raise

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
            self._log('error', "Missing entityId", method=payment_method_code)
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

        self._log('info', "Create checkout - request", request_values=request_values)

        # Create checkout
        try:
            response_content = self.provider_id._hyperpay_make_request(request_values)
            self._log('info', "Create checkout - response", response_content=response_content)
        except Exception as e:
            self._log('error', f"Create checkout EXCEPTION: {e}", request_values=request_values)
            raise

        # Validate minimal response
        if not response_content or not response_content.get('id'):
            self._log('error', "Invalid checkout response", response_content=response_content)
            raise ValidationError(_("HyperPay: Invalid checkout response."))

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

        self._log('debug', "Rendering values ready", render_values=response_content)
        return response_content

    # ------------------------------
    # Notifications
    # ------------------------------
    def _get_tx_from_notification_data(self, provider_code, data):
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code not in ('hyperpay', 'mada'):
            return tx

        self._log('info', "Notification received", provider_code=provider_code, data=data)

        try:
            process_base = self.provider_id.get_hyperpay_urls()['hyperpay_process_url']
            resource = data.get('resourcePath')
            payment_status_url = (process_base + resource) if resource else None
        except Exception as e:
            self._log('error', f"Notification build status URL failed: {e}", data=data)
            raise

        if not payment_status_url:
            self._log('error', "Notification missing resourcePath", data=data)
            raise ValidationError(_("HyperPay: Missing resourcePath in notification."))

        try:
            provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
            notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)
            self._log('info', "Status response", notification_data=notification_data)
        except Exception as e:
            self._log('error', f"Fetch payment status EXCEPTION: {e}", url=payment_status_url)
            raise

        reference = notification_data.get('merchantTransactionId', False)
        if not reference:
            self._log('error', "No merchantTransactionId in status response", notification_data=notification_data)
            raise ValidationError(_("HyperPay: No reference found."))

        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')])
        if not tx:
            self._log('error', "Transaction not found for reference", reference=reference)
            raise ValidationError(_("HyperPay: No transaction found matching reference %s.") % reference)

        tx._handle_hyperpay_payment_status(notification_data)
        return tx

    def _handle_hyperpay_payment_status(self, notification_data):
        status = notification_data.get('result', {}) or {}
        status_code = status.get('code')
        status_desc = status.get('description')

        self._log('info', "Handle payment status", status_code=status_code, status_description=status_desc)

        # Keep provider reference if present
        if 'id' in notification_data:
            self.provider_reference = notification_data.get('id')

        tx_status_set = False

        # SUCCESS
        if status_code and not tx_status_set:
            for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS']:
                if re.search(reg_exp, status_code):
                    self._log('info', "Status matched SUCCESS", matched_regex=reg_exp)
                    self._set_done(state_message=status_desc or "Authorised")
                    tx_status_set = True
                    break

        # SUCCESS_REVIEW -> pending
        if status_code and not tx_status_set:
            for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS_REVIEW']:
                if re.search(reg_exp, status_code):
                    self._log('warning', "Status matched SUCCESS_REVIEW (set pending)", matched_regex=reg_exp)
                    self._set_pending(state_message=status_desc or "Review")
                    tx_status_set = True
                    break

        # PENDING -> error (as per previous code behavior)
        if status_code and not tx_status_set:
            for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['PENDING']:
                if re.search(reg_exp, status_code):
                    self._log('warning', "Status matched PENDING (set error)", matched_regex=reg_exp)
                    self._set_error(state_message=status_desc or "Pending")
                    tx_status_set = True
                    break

        # WAITING -> error
        if status_code and not tx_status_set:
            for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['WAITING']:
                if re.search(reg_exp, status_code):
                    self._log('warning', "Status matched WAITING (set error)", matched_regex=reg_exp)
                    self._set_error(state_message=status_desc or "Waiting")
                    tx_status_set = True
                    break

        # REJECTED -> error
        if status_code and not tx_status_set:
            for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['REJECTED']:
                if re.search(reg_exp, status_code):
                    self._log('warning', "Status matched REJECTED (set error)", matched_regex=reg_exp)
                    self._set_error(state_message=status_desc or "Rejected")
                    tx_status_set = True
                    break

        # Unrecognized
        if not tx_status_set:
            self._log(
                'error',
                "Unrecognized payment state",
                status_code=status_code,
                status_description=status_desc,
                full_notification=notification_data,
            )
            self._set_error("HyperPay: " + _("Invalid payment status."))
