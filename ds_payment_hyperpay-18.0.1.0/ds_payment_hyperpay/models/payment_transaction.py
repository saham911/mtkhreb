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
from urllib.parse import urlparse

import odoo.exceptions
from odoo import api, models, _
from odoo.tools import format_amount
from odoo.exceptions import ValidationError
from odoo.addons.payment import utils as payment_utils
from odoo.addons.ds_payment_hyperpay import hyperpay_utils as hyperpay

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    def _validate_hyperpay_data(self, field, value):
        """ التحقق من صحة البيانات حسب متطلبات HyperPay """
        validators = {
            'email': lambda v: bool(re.match(r'^[^@]+@[^@]+\.[^@]+$', v or '')),
            'phone': lambda v: bool(re.match(r'^\+?\d{8,15}$', v or '')),
            'postcode': lambda v: bool(re.match(r'^\d{3,10}$', v or '')),
            'country': lambda v: bool(re.match(r'^[A-Z]{2}$', v or '')),
            'state': lambda v: bool(v and len(v) <= 30),
            'city': lambda v: bool(v and len(v) <= 30),
            'street': lambda v: bool(v and len(v) <= 50),
        }
        return validators.get(field, lambda _: True)(value)

    def _prepare_hyperpay_request(self):
        """ إعداد بيانات الطلب مع التحقق الصارم """
        partner = self.partner_id
        
        # التحقق من الحقول الإلزامية
        required_fields = {
            'email': ('customer.email', partner.email),
            'street': ('billing.street1', partner.street),
            'city': ('billing.city', partner.city),
            'country': ('billing.country', partner.country_id.code),
            'postcode': ('billing.postcode', partner.zip),
        }
        
        request_values = {
            'entityId': self.provider_id.hyperpay_merchant_id,
            'amount': "{:.2f}".format(self.amount),
            'currency': self.currency_id.name,
            'paymentType': 'DB',
            'merchantTransactionId': self.reference,
            'testMode': 'EXTERNAL',
            'customParameters[3DS2_enrolled]': 'true',
        }

        # إضافة الحقول الإلزامية مع التحقق
        for field, (key, value) in required_fields.items():
            value = (value or '').strip()
            if not self._validate_hyperpay_data(field, value):
                raise ValidationError(_("Invalid %s format: %s") % (field, value))
            request_values[key] = value

        # معالجة الاسم
        name_parts = (partner.name or '').strip().split()
        request_values.update({
            'customer.givenName': name_parts[0] if name_parts else '',
            'customer.surname': ' '.join(name_parts[1:]) if len(name_parts) > 1 else name_parts[0] if name_parts else ''
        })

        # إضافة الحقول الاختيارية
        if partner.state_id:
            state = (partner.state_id.code or partner.state_id.name or '').strip()
            if self._validate_hyperpay_data('state', state):
                request_values['billing.state'] = state

        if partner.phone:
            phone = re.sub(r'[^\d+]', '', (partner.phone or '').strip())
            if phone and not phone.startswith('+'):
                phone = f'+{phone}'
            if self._validate_hyperpay_data('phone', phone):
                request_values['customer.phone'] = phone

        return request_values

    def hyperpay_execute_payment(self):
        """ تنفيذ عملية الدفع مع معالجة الأخطاء المحسنة """
        try:
            request_values = self._prepare_hyperpay_request()
            _logger.info("HyperPay Request: %s", request_values)
            
            response = self.provider_id._hyperpay_make_request(request_values)
            _logger.info("HyperPay Response: %s", response)
            
            if not response.get('id'):
                raise ValidationError(_("Payment gateway error - No transaction ID received"))

            return {
                'action_url': '/payment/hyperpay',
                'checkout_id': response['id'],
                'merchantTransactionId': self.reference,
                'formatted_amount': format_amount(self.env, self.amount, self.currency_id),
                'paymentMethodCode': 'hyperpay',
                'payment_url': f"https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId={response['id']}",
                **response
            }
            
        except ValidationError:
            raise
        except Exception as e:
            _logger.error("HyperPay API Error: %s", str(e), exc_info=True)
            raise ValidationError(_("Payment processing failed. Please try again later."))

    def _get_tx_from_notification_data(self, provider_code, data):
        """ معالجة إشعار الدفع مع تسجيل مفصل """
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code != 'hyperpay':
            return tx

        _logger.info("HyperPay Notification: %s", data)
        
        try:
            if not data.get('resourcePath'):
                raise ValidationError(_("Missing payment resource path"))

            provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
            status_url = provider.get_hyperpay_urls()['hyperpay_process_url'] + data['resourcePath']
            notification_data = provider._hyperpay_get_payment_status(status_url, provider_code)
            
            _logger.info("Payment Status: %s", notification_data)
            
            reference = notification_data.get('merchantTransactionId')
            if not reference:
                raise ValidationError(_("Transaction reference missing"))

            tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')])
            if not tx:
                raise ValidationError(_("Transaction not found for reference %s") % reference)
                
            tx._process_hyperpay_status(notification_data)
            return tx
            
        except Exception as e:
            _logger.error("Notification Processing Failed: %s", str(e), exc_info=True)
            raise ValidationError(_("Could not verify payment status. Please contact support."))

    def _process_hyperpay_status(self, notification_data):
        """ معالجة حالة الدفع مع كود حالة مفصّل """
        status = notification_data.get('result', {})
        status_code = status.get('code', '')
        description = status.get('description', 'No status description')
        
        _logger.info("Processing Status: %s - %s", status_code, description)
        
        if 'id' in notification_data:
            self.provider_reference = notification_data['id']

        status_handlers = {
            'SUCCESS': self._set_done,
            'SUCCESS_REVIEW': self._set_pending,
            'PENDING': self._set_error,
            'REJECTED': self._set_error
        }

        for status_type, handler in status_handlers.items():
            if any(re.search(pattern, status_code) for pattern in hyperpay.PAYMENT_STATUS_CODES_REGEX[status_type]):
                handler(_(description))
                return

        _logger.error("Unrecognized status code: %s", status_code)
        self._set_error(_("Unknown payment status: %s") % status_code)
