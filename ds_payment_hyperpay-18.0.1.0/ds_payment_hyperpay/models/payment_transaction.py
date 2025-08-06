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
        """ توليد مرجع فريد للمعاملات """
        if provider_code == 'hyperpay':
            prefix = payment_utils.singularize_reference_prefix()
        return super()._compute_reference(provider_code, prefix=prefix, separator=separator, **kwargs)

    def _get_specific_rendering_values(self, processing_values):
        """ إعداد قيم العرض الخاصة بـ HyperPay """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'hyperpay':
            return res
        if self.currency_id.name != 'SAR':
            raise ValidationError(_("Only SAR currency is supported for HyperPay"))
        
        try:
            return self._execute_hyperpay_payment()
        except Exception as e:
            _logger.error("HyperPay payment execution failed: %s", str(e), exc_info=True)
            raise ValidationError(_("Payment processing error. Please try again later."))

    def _execute_hyperpay_payment(self):
        """ تنفيذ عملية الدفع عبر HyperPay مع جميع متطلبات API """
        provider = self.provider_id
        partner = self.partner_id
        
        # التحقق من التهيئة الأساسية
        if not provider.hyperpay_merchant_id:
            raise ValidationError(_("HyperPay merchant ID is not configured"))

        # التحقق من صحة البريد الإلكتروني
        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', partner.email or ''):
            raise ValidationError(_("Invalid email address format"))

        # إعداد بيانات الطلب الأساسية حسب متطلبات HyperPay
        request_values = {
            'entityId': provider.hyperpay_merchant_id,
            'amount': "{:.2f}".format(self.amount),
            'currency': 'SAR',  # العملة ثابتة حسب المتطلبات
            'paymentType': 'DB',  # نوع الدفع ثابت حسب المتطلبات
            'merchantTransactionId': self.reference,
            'testMode': 'EXTERNAL',  # مطلوب للوضع الاختباري
            'customParameters[3DS2_enrolled]': 'true',  # مطلوب لـ 3D Secure
            'customer.email': partner.email,
            'billing.street1': (partner.street or '')[:50],  # تقليل الطول إذا لزم الأمر
            'billing.city': (partner.city or '')[:30],
            'billing.country': (partner.country_id.code or '').upper(),
            'billing.postcode': partner.zip or '',
        }

        # معالجة الاسم حسب متطلبات givenName و surname
        name_parts = (partner.name or '').strip().split()
        request_values.update({
            'customer.givenName': name_parts[0] if name_parts else '',
            'customer.surname': ' '.join(name_parts[1:]) if len(name_parts) > 1 else (name_parts[0] if name_parts else '')
        })

        # إضافة الولاية إذا كانت متاحة
        if partner.state_id:
            request_values['billing.state'] = partner.state_id.code or partner.state_id.name or ''

        # إضافة رقم الهاتف إذا كان متاحاً
        if partner.phone:
            phone = re.sub(r'[^\d+]', '', partner.phone)
            if phone and not phone.startswith('+'):
                phone = f'+{phone}'
            request_values['customer.phone'] = phone

        _logger.info("HyperPay Request Values: %s", request_values)
        
        # إرسال الطلب إلى HyperPay
        response = provider._hyperpay_make_request(request_values)
        _logger.info("HyperPay Response: %s", response)

        if not response.get('id'):
            raise ValidationError(_("Payment gateway error - No transaction ID received"))

        # إعداد بيانات الرد مع إضافة سكريبت 3D Secure المطلوب
        return {
            'action_url': urls.url_join(provider.get_base_url(), '/payment/hyperpay'),
            'checkout_id': response['id'],
            'merchantTransactionId': self.reference,
            'formatted_amount': format_amount(self.env, self.amount, self.currency_id),
            'paymentMethodCode': 'hyperpay',
            'payment_url': "https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=%s" % response['id'],
            'wpwl_options': {'paymentTarget': '_top'},  # إضافة إعدادات 3D Secure المطلوبة
            **response
        }

    def _get_tx_from_notification_data(self, provider_code, data):
        """ معالجة إشعارات الدفع الواردة من HyperPay """
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code != 'hyperpay':
            return tx

        _logger.info("HyperPay Notification Data: %s", data)
        
        try:
            if not data.get('resourcePath'):
                raise ValidationError(_("Missing payment resource path"))

            provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
            status_url = urls.url_join(provider.get_hyperpay_urls()['hyperpay_process_url'], data['resourcePath'])
            notification_data = provider._hyperpay_get_payment_status(status_url, provider_code)
            
            reference = notification_data.get('merchantTransactionId')
            if not reference:
                raise ValidationError(_("Transaction reference not found in response"))

            tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')])
            if not tx:
                raise ValidationError(_("Transaction not found for reference %s") % reference)
                
            tx._process_hyperpay_notification(notification_data)
            return tx
            
        except Exception as e:
            _logger.error("Failed to process HyperPay notification: %s", str(e), exc_info=True)
            raise ValidationError(_("Could not verify payment status. Please contact support."))

    def _process_hyperpay_notification(self, notification_data):
        """ معالجة حالة الدفع مع كود حالة مفصّل """
        status = notification_data.get('result', {})
        status_code = status.get('code', '')
        description = status.get('description', 'No status description')
        
        _logger.info("Processing payment status: %s - %s", status_code, description)

        if 'id' in notification_data:
            self.provider_reference = notification_data['id']

        # معالجة حالات الدفع المختلفة حسب كود الحالة
        if any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS']):
            self._set_done(_(description))
        elif any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS_REVIEW']):
            self._set_pending(_(description))
        elif any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX['PENDING']):
            self._set_error(_(description))
        elif any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX['REJECTED']):
            self._set_error(_(description))
        else:
            _logger.error("Unrecognized payment status code: %s", status_code)
            self._set_error(_("Unknown payment status: %s") % status_code)
