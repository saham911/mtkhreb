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
        """ توليد مرجع فريد للمعاملات """
        if provider_code == 'hyperpay':
            prefix = payment_utils.singularize_reference_prefix()
        return super()._compute_reference(provider_code, prefix=prefix, separator=separator, **kwargs)

    def _get_specific_rendering_values(self, processing_values):
        """ إعداد قيم العرض الخاصة بـ HyperPay """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'hyperpay':
            return res
        if self.currency_id.id not in self.payment_method_id.supported_currency_ids.ids:
            raise odoo.exceptions.UserError("This currency is not supported with selected payment method.")
        return self.hyperpay_execute_payment()

    def _clean_hyperpay_data(self, value):
        """ تنظيف البيانات المرسلة إلى HyperPay """
        if not value:
            return ''
        # إزالة أي أحرف غير مرغوب فيها وتنسيق النص
        return re.sub(r'[^\w\s\-\.]', '', str(value)).strip()

    def hyperpay_execute_payment(self):
        """ تنفيذ عملية الدفع عبر HyperPay مع جميع التحسينات """
        try:
            hyperpay_provider = self.provider_id
            payment_method_code = self.payment_method_id.code

            # الحصول على معرف الكيان المناسب حسب طريقة الدفع
            entity_id = (
                hyperpay_provider.hyperpay_merchant_id_mada 
                if payment_method_code == 'mada' 
                else hyperpay_provider.hyperpay_merchant_id
            )
            if not entity_id:
                raise ValidationError(_("No entityID provided for '%s' transactions.") % payment_method_code)
            
            partner = self.partner_id
            
            # ===== التحقق من الحقول الإلزامية =====
            required_fields = {
                'email': partner.email,
                'name': partner.name,
                'street': partner.street,
                'city': partner.city,
                'country': partner.country_id,
                'zip': partner.zip,
            }
            
            missing_fields = [f for f, v in required_fields.items() if not v]
            if missing_fields:
                raise ValidationError(_("Missing required fields: %s") % ", ".join(missing_fields))
            
            # ===== تنظيف وتنسيق البيانات =====
            country_code = self._clean_hyperpay_data(partner.country_id.code).upper()
            city = self._clean_hyperpay_data(partner.city).split(',')[0]  # أخذ الجزء الأول فقط قبل الفاصلة
            street = self._clean_hyperpay_data(partner.street).replace('saudi arabia', '')
            zip_code = self._clean_hyperpay_data(partner.zip)
            
            # تنسيق رقم الهاتف
            phone = self._clean_hyperpay_data(partner.phone)
            if phone and not phone.startswith('+'):
                phone = f'+{phone}' if not phone.startswith('00') else f'+{phone[2:]}'
            
            # تقسيم الاسم إلى اسم أول واسم عائلة
            name_parts = self._clean_hyperpay_data(partner.name).split()
            given_name = name_parts[0] if name_parts else ''
            surname = ' '.join(name_parts[1:]) if len(name_parts) > 1 else given_name

            # ===== إعداد بيانات الطلب الأساسية =====
            request_values = {
                'entityId': entity_id,
                'amount': "{:.2f}".format(self.amount),
                'currency': self.currency_id.name,
                'paymentType': 'DB',
                'merchantTransactionId': self.reference,
                'testMode': 'EXTERNAL',
                'customParameters[3DS2_enrolled]': 'true',
                'customer.email': self._clean_hyperpay_data(partner.email),
                'customer.givenName': given_name,
                'customer.surname': surname,
                'billing.street1': street[:50],  # تقليل طول العنوان إذا كان طويلاً
                'billing.city': city[:30],       # تقليل طول اسم المدينة إذا كان طويلاً
                'billing.postcode': zip_code,
                'billing.country': country_code,
            }

            # ===== إضافة الحقول الاختيارية =====
            if partner.state_id:
                request_values['billing.state'] = self._clean_hyperpay_data(
                    partner.state_id.code or partner.state_id.name
                )[:30]  # تقليل طول اسم الولاية إذا كان طويلاً
            
            if phone:
                request_values['customer.phone'] = phone

            # ===== تسجيل البيانات قبل الإرسال =====
            _logger.info("Sending request to HyperPay: %s", {
                k: v for k, v in request_values.items() 
                if not k.startswith('customParameters')
            })

            # ===== إرسال الطلب إلى HyperPay =====
            response = self.provider_id._hyperpay_make_request(request_values)
            _logger.info("Received response from HyperPay: %s", response)

            if not response.get('id'):
                _logger.error("Invalid HyperPay response: %s", response)
                raise ValidationError(_("Payment service unavailable. Please try again."))

            # ===== إعداد بيانات الرد =====
            return {
                'action_url': '/payment/hyperpay',
                'checkout_id': response['id'],
                'merchantTransactionId': self.reference,
                'formatted_amount': format_amount(self.env, self.amount, self.currency_id),
                'paymentMethodCode': payment_method_code,
                'payment_url': f"https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId={response['id']}",
                **response
            }

        except Exception as e:
            _logger.error("HyperPay processing failed: %s", str(e), exc_info=True)
            raise ValidationError(_("Payment processing error. Please contact support."))
    
    def _get_tx_from_notification_data(self, provider_code, data):
        """ معالجة إشعارات الدفع الواردة من HyperPay """
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code not in ('hyperpay', 'mada'):
            return tx
            
        _logger.info("Received HyperPay notification: %s", data)
        
        if not data.get('resourcePath'):
            _logger.error("Missing resourcePath in notification data")
            raise ValidationError(_("Invalid payment notification"))
            
        try:
            payment_status_url = self.provider_id.get_hyperpay_urls()['hyperpay_process_url'] + data['resourcePath']
            provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
            notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)
            _logger.info("HyperPay payment status: %s", notification_data)
            
            reference = notification_data.get('merchantTransactionId')
            if not reference:
                _logger.error("No reference in payment status: %s", notification_data)
                raise ValidationError(_("Payment reference missing"))
                
            tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')])
            if not tx:
                _logger.error("Transaction not found for reference: %s", reference)
                raise ValidationError(_("Payment transaction not found"))
                
            tx._handle_hyperpay_payment_status(notification_data)
            return tx
            
        except Exception as e:
            _logger.error("Failed to process HyperPay notification: %s", str(e))
            raise ValidationError(_("Could not verify payment status"))

    def _handle_hyperpay_payment_status(self, notification_data):
        """ معالجة حالات الدفع المختلفة من HyperPay """
        if 'id' in notification_data:
            self.provider_reference = notification_data['id']

        if not notification_data.get('result'):
            _logger.error("No result in payment status: %s", notification_data)
            self._set_error(_("Invalid payment status"))
            return

        status = notification_data['result']
        status_code = status.get('code', '')
        description = status.get('description', 'No description')
        
        _logger.info("Processing payment status: %s - %s", status_code, description)

        # قاموس لحالات الدفع المختلفة
        status_handlers = {
            'SUCCESS': self._set_done,
            'SUCCESS_REVIEW': self._set_pending,
            'PENDING': self._set_error,
            'REJECTED': self._set_error
        }

        # البحث عن الحالة المناسبة
        handled = False
        for status_type, handler in status_handlers.items():
            if any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX[status_type]):
                handler(description)
                handled = True
                break

        if not handled:
            _logger.error("Unrecognized payment status: %s", status_code)
            self._set_error(_("Unknown payment status"))
