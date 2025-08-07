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

    def hyperpay_execute_payment(self):
        hyperpay_provider = self.provider_id
        payment_method_code = self.payment_method_id.code

        # اختر الـ entityId حسب MADA أو Visa/Master
        entity_id = (hyperpay_provider.hyperpay_merchant_id_mada
                     if payment_method_code == 'mada'
                     else hyperpay_provider.hyperpay_merchant_id)
        if not entity_id:
            raise ValidationError("No entityID provided for '%s' transactions." % payment_method_code)

        # URL مطلق للرجوع بعد 3DS
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url').rstrip('/')
        shopper_result_url = urls.url_join(base_url + '/', 'payment/hyperpay/return')

        # --- بيانات العميل من شريك Odoo ---
        partner = self.partner_id
        email = (partner.email or '').strip()
        full_name = (partner.name or '').strip()
        given_name = full_name.split(' ', 1)[0] if full_name else ''
        surname = full_name.split(' ', 1)[1] if ' ' in full_name else ''

        street1 = (partner.street or '').strip()
        city = (partner.city or '').strip()
        state = (partner.state_id.code or partner.state_id.name or '').strip() if partner.state_id else ''
        country = (partner.country_id.code or '').strip()  # ISO Alpha-2
        postcode = (partner.zip or '').strip()

        # Fallback بسيط للـ state (لا ترسله فاضي)
        if not state and country == 'SA':
            state = 'Riyadh'  # عدّلها إن رغبت حسب عنوان العميل

        # --- القيم الأساسية للطلب ---
        request_values = {
            'entityId': str(entity_id),
            'amount': f"{self.amount:.2f}",
            'currency': self.currency_id.name,
            'paymentType': 'DB',
            'merchantTransactionId': self.reference,  # unique ID في DB

            # بيانات العميل
            'customer.email': email or '',
            'customer.givenName': given_name or '',
            'customer.surname': surname or '',

            # الرجوع على دومينك بعد 3DS
            'shopperResultUrl': shopper_result_url,
        }

        # لا ترسل مفاتيح بقيم فارغة في الفوترة
        if street1:
            request_values['billing.street1'] = street1
        if city:
            request_values['billing.city'] = city
        if state:
            request_values['billing.state'] = state
        if country:
            request_values['billing.country'] = country
        if postcode:
            request_values['billing.postcode'] = postcode

        # وضع الاختبار فقط
        if hyperpay_provider.state != 'enabled':
            request_values['testMode'] = 'EXTERNAL'
            request_values['customParameters[3DS2_enrolled]'] = 'true'

        # تنفيذ الطلب لإنشاء checkout_id
        response_content = self.provider_id._hyperpay_make_request(request_values)

        # احفظ الـ checkout_id كـ provider_reference للعثور على المعاملة لاحقاً
        checkout_id = response_content.get('id')
        if checkout_id:
            self.provider_reference = checkout_id

        # تجهيز قيم واجهة الدفع
        response_content['action_url'] = '/payment/hyperpay'
        response_content['checkout_id'] = response_content.get('id')
        response_content['merchantTransactionId'] = response_content.get('merchantTransactionId')
        response_content['formatted_amount'] = format_amount(self.env, self.amount, self.currency_id)
        response_content['paymentMethodCode'] = payment_method_code
        response_content['return_url'] = shopper_result_url

        if hyperpay_provider.state == 'enabled':
            payment_url = f"https://eu-prod.oppwa.com/v1/paymentWidgets.js?checkoutId={response_content['checkout_id']}"
        else:
            payment_url = f"https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId={response_content['checkout_id']}"
        response_content['payment_url'] = payment_url
        return response_content

    def _get_tx_from_notification_data(self, provider_code, data):
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code not in ('hyperpay', 'mada'):
            return tx

        payment_status_url = self.provider_id.get_hyperpay_urls()['hyperpay_process_url'] + data.get('resourcePath')
        provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
        notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)
        _logger.info("HyperPay notification_data: %s", notification_data)

        # 1) المحاولة الأساسية: merchantTransactionId
        reference = notification_data.get('merchantTransactionId')
        if reference:
            tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')], limit=1)
            if not tx:
                raise ValidationError(_("HyperPay: No transaction found matching reference %s.") % reference)
            tx._handle_hyperpay_payment_status(notification_data)
            return tx

        # 2) Fallback: ابحث بالـ checkout_id المخزّن كـ provider_reference
        # يأتي من باراميترات الريديركت (id)، أو من رد الحالة (ndc)، أو من resourcePath
        checkout_id = data.get('id') or notification_data.get('ndc')
        if not checkout_id:
            # محاولة أخيرة: استخرج الـ id من resourcePath: /v1/checkouts/<ID>/payment
            rp = data.get('resourcePath') or ''
            parts = rp.split('/checkouts/')
            if len(parts) == 2:
                checkout_id = parts[1].split('/payment')[0]

        if checkout_id:
            tx = self.search([('provider_reference', '=', checkout_id), ('provider_code', '=', 'hyperpay')], limit=1)
            if tx:
                tx._handle_hyperpay_payment_status(notification_data)
                return tx

        # إذا ما قدرنا نحددها بأي شكل
        raise ValidationError(_("HyperPay: No reference found."))

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
