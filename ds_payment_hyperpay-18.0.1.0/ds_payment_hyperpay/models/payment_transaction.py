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

    def hyperpay_execute_payment(self):
        hyperpay_provider = self.provider_id
        payment_method_code = self.payment_method_id.code

        if payment_method_code == 'mada':
            entity_id = hyperpay_provider.hyperpay_merchant_id_mada
        else:
            entity_id = hyperpay_provider.hyperpay_merchant_id
        if not entity_id:
            raise ValidationError("No entityID provided for '%s' transactions." % payment_method_code)

        # تحقق من وجود بيانات العميل الأساسية
        partner = self.partner_id
        if not partner.email:
            raise ValidationError("Customer email is required for HyperPay payments")
        
        # استخراج اسم العميل
        name_parts = partner.name.split()
        given_name = name_parts[0] if name_parts else 'Customer'
        surname = ' '.join(name_parts[1:]) if len(name_parts) > 1 else given_name

        # إعداد بيانات العنوان مع قيم افتراضية
        street = partner.street or 'Not Provided'
        city = partner.city or 'Not Provided'
        state = partner.state_id.name or partner.state or 'Not Provided'
        country = partner.country_id.code or 'SA'  # SA كقيمة افتراضية للمملكة العربية السعودية
        zip_code = partner.zip or '00000'

        # بناء قيم الطلب
        request_values = {
            'entityId': str(entity_id),
            'amount': "{:.2f}".format(self.amount),
            'currency': self.currency_id.name,
            'paymentType': 'DB',
            'merchantTransactionId': self.reference,
            # إضافة المعلمات الإلزامية
            'customer.email': partner.email,
            'billing.street1': street,
            'billing.city': city,
            'billing.state': state,
            'billing.country': country,
            'billing.postcode': zip_code,
            'customer.givenName': given_name,
            'customer.surname': surname,
        }

        # إضافة معلمات الاختبار للبيئات غير الإنتاجية
        if hyperpay_provider.state != 'enabled':  # Test environment
            request_values.update({
                'testMode': 'EXTERNAL',
                'customParameters[3DS2_enrolled]': 'true',
            })

        try:
            # تسجيل الطلب قبل الإرسال
            _logger.info("Sending HyperPay request: %s", request_values)
            
            response_content = self.provider_id._hyperpay_make_request(request_values)
            
            # تسجيل الاستجابة
            _logger.info("HyperPay API response: %s", response_content)
            
            if not response_content.get('id'):
                error_msg = response_content.get('result', {}).get('description', 'Unknown error')
                _logger.error("HyperPay payment failed: %s", error_msg)
                raise ValidationError(_("HyperPay payment failed: %s") % error_msg)
                
        except Exception as e:
            _logger.exception("HyperPay API exception: %s", str(e))
            raise ValidationError(_("Could not process HyperPay payment: %s") % str(e))

        # بناء رابط الدفع
        if hyperpay_provider.state == 'enabled':
            base_url = "https://eu-prod.oppwa.com/v1/paymentWidgets.js?checkoutId="
        else:
            base_url = "https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId="
        
        payment_url = base_url + response_content['id']
        
        # إرجاع البيانات المطلوبة للتقديم
        return {
            'action_url': '/payment/hyperpay',
            'checkout_id': response_content['id'],
            'merchantTransactionId': response_content.get('merchantTransactionId', self.reference),
            'formatted_amount': format_amount(self.env, self.amount, self.currency_id),
            'paymentMethodCode': payment_method_code,
            'payment_url': payment_url,
            'amount': self.amount,
            'currency': self.currency_id.name,
        }

    def _get_tx_from_notification_data(self, provider_code, data):
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code not in ('hyperpay', 'mada'):
            return tx
        
        # تسجيل بيانات الإشعار الواردة
        _logger.info("HyperPay notification data: %s", data)
        
        payment_status_url = self.provider_id.get_hyperpay_urls()['hyperpay_process_url'] + data.get('resourcePath')
        provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
        notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)
        
        # تسجيل بيانات حالة الدفع
        _logger.info("HyperPay payment status: %s", notification_data)
        
        reference = notification_data.get('merchantTransactionId', False)
        if not reference:
            raise ValidationError(_("HyperPay: No reference found."))
        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')])
        if not tx:
            raise ValidationError(_("HyperPay: No transaction found matching reference %s.") % reference)
        tx._handle_hyperpay_payment_status(notification_data)
        return tx

    def _handle_hyperpay_payment_status(self, notification_data):
        # تسجيل حالة الدفع الواردة
        _logger.info("Handling HyperPay payment status: %s", notification_data)
        
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
                        _logger.info("Transaction %s set to DONE with status: %s", self.reference, status_code)
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS_REVIEW']:
                    if re.search(reg_exp, status_code):
                        self._set_pending(state_message=status.get('description'))
                        tx_status_set = True
                        _logger.info("Transaction %s set to PENDING with status: %s", self.reference, status_code)
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['PENDING']:
                    if re.search(reg_exp, status_code):
                        self._set_error(state_message=status.get('description'))
                        tx_status_set = True
                        _logger.warning("Transaction %s set to ERROR with status: %s", self.reference, status_code)
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['WAITING']:
                    if re.search(reg_exp, status_code):
                        self._set_error(state_message=status.get('description'))
                        tx_status_set = True
                        _logger.warning("Transaction %s set to ERROR with status: %s", self.reference, status_code)
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['REJECTED']:
                    if re.search(reg_exp, status_code):
                        self._set_error(state_message=status.get('description'))
                        tx_status_set = True
                        _logger.warning("Transaction %s set to ERROR with status: %s", self.reference, status_code)
                        break

            if not tx_status_set:
                _logger.warning("Received unrecognized payment state %s for "
                                "transaction with reference %s\nDetailed Message:%s", status_code, self.reference,
                                status.get('description'))
                self._set_error("HyperPay: " + _("Invalid payment status."))
