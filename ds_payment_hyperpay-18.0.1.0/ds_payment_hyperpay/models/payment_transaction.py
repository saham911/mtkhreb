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
    
        # Get partner information
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
        
        missing_fields = [field for field, value in required_fields.items() if not value]
        if missing_fields:
            raise ValidationError(_("Missing required fields for HyperPay: %s") % ", ".join(missing_fields))
        
        # تأكيد تنسيق كود الدولة
        if partner.country_id and len(partner.country_id.code or '') != 2:
            raise ValidationError(_("Country code must be 2 characters (ISO Alpha-2)"))
        
        # Split full name into given name and surname
        full_name = partner.name.strip().split()
        given_name = full_name[0] if full_name else ''
        surname = ' '.join(full_name[1:]) if len(full_name) > 1 else given_name
        
        # Prepare base request values
        request_values = {
            'entityId': '%s' % entity_id,
            'amount': "{:.2f}".format(self.amount),
            'currency': self.currency_id.name,
            'paymentType': 'DB',
            'merchantTransactionId': self.reference,
            'testMode': 'EXTERNAL',  # Only for test server
            'customParameters[3DS2_enrolled]': 'true',  # Only for test server
            'customer.email': partner.email,
            'customer.givenName': given_name,
            'customer.surname': surname,
            'billing.street1': partner.street,
            'billing.city': partner.city,
            'billing.postcode': partner.zip.replace(' ', ''),
            'billing.country': partner.country_id.code.upper(),
            'customer.ip': self.env['payment.transaction']._get_customer_ip_address() or '',
        }

        # Handle state if available
        if partner.state_id:
            request_values['billing.state'] = partner.state_id.code or partner.state_id.name or ''

        # Add phone number if available
        if partner.phone:
            request_values['customer.phone'] = partner.phone.replace(' ', '').replace('+', '')

        _logger.info("HyperPay Request Data: %s", request_values)  # تسجيل البيانات المرسلة
        response_content = self.provider_id._hyperpay_make_request(request_values)
        _logger.info("HyperPay Response: %s", response_content)  # تسجيل الرد المستلم

        if not response_content.get('id'):
            raise ValidationError(_("HyperPay: No checkout ID received in response"))

        response_content['action_url'] = '/payment/hyperpay'
        response_content['checkout_id'] = response_content.get('id')
        response_content['merchantTransactionId'] = self.reference  # استخدام المرجع المحلي بدلاً من القيمة المرجعة
        response_content['formatted_amount'] = format_amount(self.env, self.amount, self.currency_id)
        response_content['paymentMethodCode'] = payment_method_code
        
        if hyperpay_provider.state == 'enabled':
            payment_url = "https://eu-prod.oppwa.com/v1/paymentWidgets.js?checkoutId=%s" % response_content['checkout_id']
        else:
            payment_url = "https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=%s" % response_content['checkout_id']
        
        response_content['payment_url'] = payment_url
        return response_content
    
    def _get_tx_from_notification_data(self, provider_code, data):
        tx = super()._get_tx_from_notification_data(provider_code, data)
        if provider_code not in ('hyperpay', 'mada'):
            return tx
            
        # تسجيل بيانات الإشعار للتحليل
        _logger.info("HyperPay Notification Data: %s", data)
        
        payment_status_url = self.provider_id.get_hyperpay_urls()['hyperpay_process_url'] + data.get('resourcePath')
        provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
        notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)
        
        # تسجيل بيانات حالة الدفع
        _logger.info("HyperPay Payment Status Data: %s", notification_data)
        
        reference = notification_data.get('merchantTransactionId', False)
        if not reference:
            raise ValidationError(_("HyperPay: No reference found in notification data"))
            
        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')])
        if not tx:
            raise ValidationError(_("HyperPay: No transaction found matching reference %s.") % reference)
            
        # معالجة حالة الدفع
        tx._handle_hyperpay_payment_status(notification_data)
        return tx

    def _handle_hyperpay_payment_status(self, notification_data):
        tx_status_set = False
        status = notification_data.get('result', False)
        
        if 'id' in notification_data:
            self.provider_reference = notification_data.get('id', False)

        if status and 'code' in status:
            status_code = status.get('code')
            description = status.get('description', 'No description')
            
            _logger.info("Processing HyperPay payment status: %s - %s", status_code, description)
            
            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS']:
                    if re.search(reg_exp, status_code):
                        self._set_done(state_message=description)
                        tx_status_set = True
                        _logger.info("Transaction marked as DONE: %s", description)
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['SUCCESS_REVIEW']:
                    if re.search(reg_exp, status_code):
                        self._set_pending(state_message=description)
                        tx_status_set = True
                        _logger.info("Transaction marked as PENDING: %s", description)
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['PENDING']:
                    if re.search(reg_exp, status_code):
                        self._set_error(state_message=description)
                        tx_status_set = True
                        _logger.warning("Transaction marked as ERROR (PENDING): %s", description)
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['WAITING']:
                    if re.search(reg_exp, status_code):
                        self._set_error(state_message=description)
                        tx_status_set = True
                        _logger.warning("Transaction marked as ERROR (WAITING): %s", description)
                        break

            if not tx_status_set:
                for reg_exp in hyperpay.PAYMENT_STATUS_CODES_REGEX['REJECTED']:
                    if re.search(reg_exp, status_code):
                        self._set_error(state_message=description)
                        tx_status_set = True
                        _logger.error("Transaction marked as ERROR (REJECTED): %s", description)
                        break

            if not tx_status_set:
                _logger.error("Unrecognized payment state %s for transaction %s: %s", 
                             status_code, self.reference, description)
                self._set_error("HyperPay: " + _("Invalid payment status: %s") % description)
