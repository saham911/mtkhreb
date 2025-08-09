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

        partner = self.partner_id
    
            # التحقق من البيانات الأساسية
        if not partner.email or '@' not in partner.email:
                raise ValidationError(_("Invalid customer email"))
    
        # معالجة الحقول
        country_code = (partner.country_id.code or '').upper()
        if not country_code or len(country_code) != 2:
                raise ValidationError(_("Country code must be 2 characters (e.g. SA)"))
    
        state_code = (partner.state_id.code or partner.state_id.name or '')[:3]
        zip_code = re.sub(r'[^a-zA-Z0-9]', '', partner.zip or '00000')[:10]
    
        name_parts = (partner.name or '').strip().split()
        given_name = name_parts[0] if name_parts else ''
        surname = ' '.join(name_parts[1:]) if len(name_parts) > 1 else given_name

        request_values = {
                'entityId': entity_id,
                'amount': "{:.2f}".format(self.amount),
                'currency': self.currency_id.name,
                'paymentType': 'DB',
                'merchantTransactionId': self.reference,
                'customer.email': partner.email,
                'billing.street1': partner.street or '',
                'billing.city': partner.city or '',
                'billing.state': state_code,
                'billing.country': country_code,
                'billing.postcode': zip_code,
                'customer.givenName': given_name[:50],
                'customer.surname': surname[:50],
            }

        if hyperpay_provider.state != 'enabled':
                request_values['testMode'] = 'EXTERNAL'
                request_values['customParameters[3DS2_enrolled]'] = 'true'

            # تسجيل البيانات المرسلة للتشخيص
        _logger.info("Sending to HyperPay: %s", request_values)
    
        response_content = self.provider_id._hyperpay_make_request(request_values)
    
        # تسجيل الاستجابة
        _logger.info("HyperPay response: %s", response_content)

        response_content['action_url'] = '/payment/hyperpay'
        response_content['checkout_id'] = response_content.get('id')
        response_content['merchantTransactionId'] = response_content.get('merchantTransactionId')
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
        payment_status_url = self.provider_id.get_hyperpay_urls()['hyperpay_process_url'] + data.get('resourcePath')
        provider = self.env['payment.provider'].search([('code', '=', 'hyperpay')], limit=1)
        notification_data = provider._hyperpay_get_payment_status(payment_status_url, provider_code)
        reference = notification_data.get('merchantTransactionId', False)
        if not reference:
            raise ValidationError(_("HyperPay: No reference found."))
        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'hyperpay')])
        if not tx:
            raise ValidationError(_("HyperPay: No transaction found matching reference %s.") % reference)
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
                _logger.warning("Received unrecognized payment state %s for "
                                "transaction with reference %s\nDetailed Message:%s", status_code, self.reference,
                                status.get('description'))
                self._set_error("HyperPay: " + _("Invalid payment status."))
