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

        if payment_method_code == 'mada':
            entity_id = hyperpay_provider.hyperpay_merchant_id_mada
        else:
            entity_id = hyperpay_provider.hyperpay_merchant_id
        if not entity_id:
            raise ValidationError("No entityID provided for '%s' transactions." % payment_method_code)

        # معالجة الاسم الكامل للحصول على الاسم الأول واللقب
        partner = self.partner_id
        name_parts = (partner.name or 'Test User').strip().split()
        given_name = name_parts[0] if name_parts else 'Test'
        surname = ' '.join(name_parts[1:]) if len(name_parts) > 1 else 'User'
        
        # معالجة العنوان
        street = partner.street or 'Al arid, Abubaker'
        city = partner.city or 'Riyadh'
        zip_code = partner.zip or '11322'  # الرمز البريدي للبطاقة الاختبارية
        
        # بيانات الطلب الأساسية
        request_values = {
            'entityId': entity_id,
            'amount': "{:.2f}".format(self.amount),
            'currency': self.currency_id.name,
            'paymentType': 'DB',
            'merchantTransactionId': self.reference,
            
            # معلمات الاختبار الإلزامية
            'testMode': 'EXTERNAL',
            'customParameters[3DS2_enrolled]': 'true',
            
            # معلومات العميل (يمكن استخدام بيانات الاختبار الثابتة)
            'customer.email': partner.email or 'test@example.com',
            'customer.givenName': given_name,
            'customer.surname': surname,
            
            # معلومات الفاتورة (يجب أن تطابق البطاقة الاختبارية)
            'billing.street1': street,
            'billing.city': city,
            'billing.state': 'RUH',  # كود المنطقة للرياض
            'billing.country': 'SA',
            'billing.postcode': zip_code,
        }

        # تسجيل البيانات المرسلة للتحقق
        _logger.info("🔁 HyperPay Request Payload for transaction [%s]: %s", 
                    self.reference, request_values)

        try:
            response_content = self.provider_id._hyperpay_make_request(request_values)
        except Exception as e:
            _logger.error("HyperPay API Error: %s", str(e))
            raise ValidationError(_("Payment processing failed. Please try again."))

        # معالجة الرد
        if not response_content.get('id'):
            _logger.error("HyperPay Invalid Response: %s", response_content)
            raise ValidationError(_("Invalid response from payment gateway."))

        response_content.update({
            'action_url': '/payment/hyperpay',
            'checkout_id': response_content['id'],
            'merchantTransactionId': self.reference,
            'formatted_amount': format_amount(self.env, self.amount, self.currency_id),
            'paymentMethodCode': payment_method_code,
            'payment_url': "https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=%s" % response_content['id']
        })

        return response_content

    def _get_tx_from_notification_data(self, provider_code, data):
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code not in ('hyperpay', 'mada'):
            return tx
            
        payment_status_url = self.provider_id.get_hyperpay_urls()['hyperpay_process_url'] + data.get('resourcePath')
        provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
        
        try:
            notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)
        except Exception as e:
            _logger.error("HyperPay Status Check Failed: %s", str(e))
            raise ValidationError(_("Could not verify payment status. Please contact support."))

        reference = notification_data.get('merchantTransactionId')
        if not reference:
            _logger.error("HyperPay Missing Reference in: %s", notification_data)
            raise ValidationError(_("HyperPay: No reference found."))

        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')])
        if not tx:
            _logger.error("HyperPay Transaction Not Found: %s", reference)
            raise ValidationError(_("HyperPay: No transaction found matching reference %s.") % reference)
            
        tx._handle_hyperpay_payment_status(notification_data)
        return tx

    def _handle_hyperpay_payment_status(self, notification_data):
        if 'id' in notification_data:
            self.provider_reference = notification_data['id']

        status = notification_data.get('result', {})
        status_code = status.get('code', '')
        description = status.get('description', 'No description')

        if not status_code:
            _logger.error("HyperPay Missing Status Code: %s", notification_data)
            self._set_error("HyperPay: " + _("Invalid payment status."))
            return

        # معالجة حالات الدفع المختلفة
        for state, regex_list in hyperpay.PAYMENT_STATUS_CODES_REGEX.items():
            for regex in regex_list:
                if re.search(regex, status_code):
                    if state == 'SUCCESS':
                        self._set_done(state_message=description)
                    elif state == 'SUCCESS_REVIEW':
                        self._set_pending(state_message=description)
                    else:
                        self._set_error(state_message=description)
                    return

        _logger.warning("Unrecognized HyperPay status %s: %s", status_code, description)
        self._set_error("HyperPay: " + _("Unknown payment status."))
