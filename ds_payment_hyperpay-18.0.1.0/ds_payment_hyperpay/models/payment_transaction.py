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
from odoo.http import request as http_request  # لالتقاط IP العميل

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
        """Build HyperPay checkout request with strict filtering:
        - Only required & safe fields + defaults
        - No billing.state for SA (and most countries)
        - Sanitize merchantTransactionId (alphanumeric, <=30)
        - Add customer.mobile (mandatory one-of) & customer.ip (IPv4 if possible)
        - Try extra IDs; fallback without them if checkout creation rejects
        """
        provider = self.provider_id
        pm_code  = self.payment_method_id.code

        entity_id = provider.hyperpay_merchant_id_mada if pm_code == 'mada' else provider.hyperpay_merchant_id
        if not entity_id:
            raise ValidationError("No entityID provided for '%s' transactions." % pm_code)

        partner = self.partner_id

        # ---------- helpers ----------
        import re as _re
        def _clean(v): return (v or "").strip()
        def _ascii_safe(text):
            s = _clean(text)
            s = _re.sub(r'\s+', ' ', s)
            try:
                from unidecode import unidecode
                s = unidecode(s)
            except Exception:
                s = s.encode('ascii', 'ignore').decode()
            return _re.sub(r'[^A-Za-z0-9 .,\-/#]', '', s).strip()
        def _split_name(fullname):
            fullname = _clean(fullname)
            if not fullname: return "", ""
            parts = fullname.split()
            return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (parts[0], "")
        def _ensure_street_ok(street):
            s = _ascii_safe(street)
            if len(s) < 5 or not _re.search(r'\d', s): s = "King Fahd Road 123"
            return s
        def _ensure_city_ok(city):
            c = _ascii_safe(city)
            return c if len(c) >= 2 else "Riyadh"
        def _ensure_postcode_ok(zipcode):
            z = _re.sub(r'\D', '', _clean(zipcode))
            return z if 4 <= len(z) <= 10 else "11322"

        # phone formatter (+ccc-nnnnnnnn) مع تفضيل السعودية
        def _format_mobile(partner_rec):
            raw = (partner_rec.mobile or partner_rec.phone or "").strip()
            digits = re.sub(r'\D', '', raw)
            if not digits:
                return ""
            # لو سعودية:
            is_sa = bool(partner_rec.country_id and partner_rec.country_id.code == 'SA')
            if is_sa:
                # 05XXXXXXXX -> +966-5XXXXXXXX
                if digits.startswith('05'):
                    digits = '5' + digits[2:]
                # 5XXXXXXXX -> +966-5XXXXXXXX
                if digits.startswith('5'):
                    return f"+966-{digits}"
                # 9665XXXXXXXX -> +966-5XXXXXXXX
                if digits.startswith('966'):
                    rest = digits[3:]
                    if rest.startswith('0'):
                        rest = rest[1:]
                    return f"+966-{rest}"
                # أي رقم آخر سعودي: أزل الصفر الأول
                if digits.startswith('0'):
                    digits = digits[1:]
                return f"+966-{digits}"
            # غير السعودية: حاول تقسيمه كـ +ccc-...
            if len(digits) > 3:
                return f"+{digits[:3]}-{digits[3:]}"
            return f"+{digits}-"

        # استخرج IPv4 من الطلب (X-Forwarded-For أو remote_addr)
        def _get_ipv4():
            try:
                if http_request:
                    xff = http_request.httprequest.headers.get('X-Forwarded-For', '') or ''
                    cand = (xff.split(',')[0] or '').strip() or http_request.httprequest.remote_addr or ''
                    m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})', cand)
                    return m.group(1) if m else ""
            except Exception:
                pass
            return ""

        # ---------- names ----------
        given_raw   = getattr(partner, 'x_customer_givenname', '') or getattr(partner, 'firstname', '')
        surname_raw = getattr(partner, 'x_customer_surname', '') or getattr(partner, 'lastname', '')
        if not given_raw or not surname_raw:
            s_given, s_surname = _split_name(partner.name or '')
            given_raw   = given_raw   or s_given
            surname_raw = surname_raw or s_surname
        given   = _ascii_safe(given_raw) or "Customer"
        surname = _ascii_safe(surname_raw) or "Name"

        # ---------- address ----------
        street1  = _ensure_street_ok(getattr(partner, 'x_billing_street1', '') or partner.street)
        city     = _ensure_city_ok(getattr(partner, 'x_billing_city', '')    or partner.city)
        postcode = _ensure_postcode_ok(getattr(partner, 'x_billing_postcode', '') or partner.zip)
        country_code = (partner.country_id and (partner.country_id.code or '')) or ''
        country_code = (country_code or 'SA').upper()[:2]

        email_val = _clean(partner.email) or "no-reply@example.com"
        mobile_val = _format_mobile(partner)  # قد يرجع فارغًا لو ما فيه رقم
        ip_val = _get_ipv4()                  # قد يرجع فارغًا لو ما قدر يستخرج IPv4

        # ---------- sanitize merchantTransactionId ----------
        raw_ref = self.reference or ""
        merchant_tx_id = re.sub(r'[^A-Za-z0-9]', '', raw_ref) or "TX"
        merchant_tx_id = merchant_tx_id[:30]

        # Optional IDs (قد ترفضها بعض القنوات؛ عندنا fallback)
        invoice_ref = None
        try:
            if getattr(self, 'sale_order_ids', False):
                invoice_ref = self.sale_order_ids[:1].name
        except Exception:
            invoice_ref = None
        if not invoice_ref:
            try:
                if getattr(self, 'invoice_ids', False):
                    invoice_ref = self.invoice_ids[:1].name
            except Exception:
                invoice_ref = None
        merchant_invoice_id = re.sub(r'[^A-Za-z0-9]', '', (invoice_ref or merchant_tx_id))[:30]
        merchant_customer_id = str(partner.id)
        currency_code = (self.currency_id.name or "SAR").upper()[:3]

        # ---------- base payload ----------
        base_values = {
            'entityId': '%s' % entity_id,
            'amount': "{:.2f}".format(self.amount),
            'currency': currency_code,
            'paymentType': 'DB',
            'merchantTransactionId': merchant_tx_id,

            # customer
            'customer.email': email_val,
            'customer.givenName': given,
            'customer.surname': surname,

            # address (minimal)
            'billing.street1': street1,
            'billing.city': city,
            'billing.country': country_code,
            'billing.postcode': postcode,
        }
        # أضف الجوال و IP فقط إن توفّرت قيمتهما
        if mobile_val:
            base_values['customer.mobile'] = mobile_val
        if ip_val:
            base_values['customer.ip'] = ip_val

        # test flags
        if provider.state != 'enabled':
            base_values['testMode'] = 'EXTERNAL'
            base_values['customParameters[3DS2_enrolled]'] = 'true'

        # مفاتيح اختيارية قد تُرفض من بعض القنوات؛ سنحاول بها أولًا ثم نfallback
        extra_ids = {
            'merchantCustomerId': merchant_customer_id,
            'merchantInvoiceId': merchant_invoice_id,
        }

        # Attempt #1 (with extra ids)
        request_values = dict(base_values, **extra_ids)
        _logger.info("HyperPay payload attempt#1: %s", {k: v for k, v in request_values.items() if k not in ('entityId',)})
        response_content = provider._hyperpay_make_request(request_values)
        _logger.info("HyperPay create-checkout response#1: %s", response_content)
        checkout_id = response_content.get('id')

        # Fallback #2 (without extra ids)
        if not checkout_id:
            request_values = dict(base_values)
            _logger.info("HyperPay payload attempt#2 (fallback, no extra ids): %s", {k: v for k, v in request_values.items() if k not in ('entityId',)})
            response_content = provider._hyperpay_make_request(request_values)
            _logger.info("HyperPay create-checkout response#2: %s", response_content)
            checkout_id = response_content.get('id')

        if not checkout_id:
            desc = (response_content.get('result') or {}).get('description') or "Unknown error"
            code = (response_content.get('result') or {}).get('code') or "N/A"
            raise odoo.exceptions.UserError(_("HyperPay checkout creation failed: %s (%s)") % (desc, code))

        # Prepare rendering values
        response_content.update({
            'action_url': '/payment/hyperpay',
            'checkout_id': checkout_id,
            'merchantTransactionId': response_content.get('merchantTransactionId') or merchant_tx_id,
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
        """Map HyperPay callback to our tx even if merchantTransactionId was sanitized."""
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

        ref = notification_data.get('merchantTransactionId', False)
        if not ref:
            raise ValidationError(_("HyperPay: No reference found."))

        # Exact match
        tx = self.search([('reference', '=', ref), ('provider_code', '=', 'hyperpay')], limit=1)

        # Fallback: sanitized/unsanitized variants
        if not tx:
            candidates = set()
            candidates.add(ref)
            candidates.add(re.sub(r'[^A-Za-z0-9]', '', ref))  # remove punctuation
            if ref.startswith('tx') and '-' not in ref and len(ref) > 2:
                candidates.add('tx-' + ref[2:])
            if '-' in ref:
                candidates.add(ref.replace('-', ''))
            tx = self.search([('reference', 'in', list(candidates)), ('provider_code', '=', 'hyperpay')], limit=1)

        if not tx:
            raise ValidationError(_("HyperPay: No transaction found matching reference %s.") % ref)

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
