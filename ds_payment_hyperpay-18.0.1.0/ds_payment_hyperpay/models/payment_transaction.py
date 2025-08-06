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

    def _get_customer_ip(self):
        """الحصول على عنوان IP العميل بطريقة آمنة"""
        try:
            request = self.env['ir.http']._get_request()
            if request:
                return request.httprequest.remote_addr
        except Exception as e:
            _logger.warning("Failed to get customer IP: %s", str(e))
        return None

    def hyperpay_execute_payment(self):
        hyperpay_provider = self.provider_id
        payment_method_code = self.payment_method_id.code

        if payment_method_code == 'mada':
            entity_id = hyperpay_provider.hyperpay_merchant_id_mada
        else:
            entity_id = hyperpay_provider.hyperpay_merchant_id
        if not entity_id:
            raise ValidationError("No entityID provided for '%s' transactions." % payment_method_code)
    
        # الحصول على بيانات العميل
        partner = self.partner_id
        
        # التحقق من الحقول الإلزامية
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
        
        # التحقق من تنسيق كود الدولة
        country_code = partner.country_id.code or ''
        if len(country_code) != 2:
            raise ValidationError(_("Country code must be 2 characters (e.g. SA for Saudi Arabia)"))
        
        # تقسيم الاسم إلى اسم أول واسم عائلة
        name_parts = partner.name.strip().split()
        given_name = name_parts[0] if name_parts else ''
        surname = ' '.join(name_parts[1:]) if len(name_parts) > 1 else given_name
        
        # الحصول على عنوان IP العميل
        customer_ip = self._get_customer_ip()
        
        # إعداد بيانات الطلب
        request_values = {
            'entityId': entity_id,
            'amount': "{:.2f}".format(self.amount),
            'currency': self.currency_id.name,
            'paymentType': 'DB',
            'merchantTransactionId': self.reference,
            'testMode': 'EXTERNAL',
            'customParameters[3DS2_enrolled]': 'true',
            'customer.email': partner.email,
            'customer.givenName': given_name,
            'customer.surname': surname,
            'billing.street1': partner.street,
            'billing.city': partner.city,
            'billing.postcode': partner.zip.replace(' ', ''),
            'billing.country': country_code.upper(),
        }

        # إضافة عنوان IP إذا كان متاحاً
        if customer_ip:
            request_values['customer.ip'] = customer_ip

        # إضافة بيانات الولاية/المحافظة إذا كانت متاحة
        if partner.state_id:
            request_values['billing.state'] = partner.state_id.code or partner.state_id.name or ''

        # إضافة رقم الهاتف إذا كان متاحاً
        if partner.phone:
            request_values['customer.phone'] = re.sub(r'[^\d]', '', partner.phone)

        _logger.info("Sending request to HyperPay: %s", request_values)
        
        try:
            response = self.provider_id._hyperpay_make_request(request_values)
            _logger.info("Received response from HyperPay: %s", response)
        except Exception as e:
            _logger.error("HyperPay API request failed: %s", str(e))
            raise ValidationError(_("Payment processing error. Please try again later."))

        if not response.get('id'):
            _logger.error("Invalid HyperPay response: %s", response)
            raise ValidationError(_("Payment service unavailable. Please try again."))

        # إعداد بيانات الرد
        response.update({
            'action_url': '/payment/hyperpay',
            'checkout_id': response['id'],
            'merchantTransactionId': self.reference,
            'formatted_amount': format_amount(self.env, self.amount, self.currency_id),
            'paymentMethodCode': payment_method_code,
            'payment_url': "https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=%s" % response['id']
        })

        return response
    
    def _get_tx_from_notification_data(self, provider_code, data):
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code not in ('hyperpay', 'mada'):
            return tx
            
        _logger.info("Received HyperPay notification: %s", data)
        
        if not data.get('resourcePath'):
            _logger.error("Missing resourcePath in notification data")
            raise ValidationError(_("Invalid payment notification"))
            
        payment_status_url = self.provider_id.get_hyperpay_urls()['hyperpay_process_url'] + data['resourcePath']
        provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
        
        try:
            notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)
            _logger.info("HyperPay payment status: %s", notification_data)
        except Exception as e:
            _logger.error("Failed to get payment status: %s", str(e))
            raise ValidationError(_("Could not verify payment status"))
        
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

    def _handle_hyperpay_payment_status(self, notification_data):
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

        # معالجة حالات الدفع المختلفة
        if any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS']):
            self._set_done(description)
        elif any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS_REVIEW']):
            self._set_pending(description)
        elif any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX['PENDING']):
            self._set_error(description)
        elif any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX['REJECTED']):
            self._set_error(description)
        else:
            _logger.error("Unrecognized payment status: %s", status_code)
            self._set_error(_("Unknown payment status"))
