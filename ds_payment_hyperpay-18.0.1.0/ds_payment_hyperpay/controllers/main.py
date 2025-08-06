# -*- coding: utf-8 -*-
#############################################################################
#
#    Copyright (C) 2024-TODAY
#    Author: Odoo DevSouls <odoodevsouls@gmailcom>
#
#############################################################################
import logging
import pprint

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class HyperPayController(http.Controller):
    _return_url = '/payment/hyperpay/return'
    _return_url_mada = '/payment/hyperpay/return_mada'

    @http.route(_return_url, type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def hyperpay_return(self, **data):
        _logger.info("handling redirection from HyperPay with data:\n%s", pprint.pformat(data))
        request.env['payment.transaction'].sudo()._handle_notification_data('hyperpay', data)
        return request.redirect('/my/orders')  # ✅ يعيد العميل إلى صفحة الطلبات بعد الدفع

    @http.route(_return_url_mada, type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def hyperpay_return_mada(self, **data):
        _logger.info("handling redirection from HyperPay with data:\n%s", pprint.pformat(data))
        request.env['payment.transaction'].sudo()._handle_notification_data('mada', data)
        return request.redirect('/my/orders')  # ✅ يعيد العميل إلى صفحة الطلبات بعد الدفع

    @http.route('/payment/hyperpay', website=True, type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def hyperpay_redirect(self, **post_data):
        provider = post_data.get('paymentMethodCode', 'hyperpay')
        base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url')
        
        form_values = {
            'payment_url': post_data.get('payment_url', False),
            'checkout_id': post_data.get('checkout_id', False),
            'amount': post_data.get('formatted_amount', False),
            'provider': provider,
        }
        if provider == 'mada':
            form_values.update({
                'return_url': f'{base_url}{self._return_url_mada}',
                'brands': 'MADA'
            })
        else:
            form_values.update({
                'return_url': f'{base_url}{self._return_url}',
                'brands': 'VISA MASTER'
            })
        return request.render('ds_payment_hyperpay.hyperpay_payment_form', form_values)
