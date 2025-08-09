# -*- coding: utf-8 -*-
#############################################################################
#
#    Copyright (C) 2024-TODAY
#    Author: Odoo DevSouls <odoodevsouls@gmailcom>
#
#############################################################################

import logging
import pprint
from werkzeug import urls

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class HyperPayController(http.Controller):
    _return_url = '/payment/hyperpay/return'
    _return_url_mada = '/payment/hyperpay/return_mada'

    @http.route(_return_url, type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def hyperpay_return(self, **data):
        _logger.info("handling redirection from HyperPay with data:\n%s", pprint.pformat(data))
        # provider_code = 'hyperpay' (Visa/Master)
        request.env['payment.transaction'].sudo()._handle_notification_data('hyperpay', data)
        return request.redirect('/payment/status')

    @http.route(_return_url_mada, type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def hyperpay_return_mada(self, **data):
        _logger.info("handling redirection from HyperPay (MADA) with data:\n%s", pprint.pformat(data))
        # provider_code = 'mada'
        request.env['payment.transaction'].sudo()._handle_notification_data('mada', data)
        return request.redirect('/payment/status')

    @http.route('/payment/hyperpay', website=True, type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def hyperpay_redirect(self, **post_data):
        """
        يستقبل قيم الـ rendering من الموديل ويعرض صفحة الـ widget.
        يتوقع الحقول:
          - payment_url (رابط سكربت الـ widget مع checkoutId)
          - checkout_id
          - formatted_amount
          - paymentMethodCode (hyperpay | mada)
        """
        _logger.info("render hyperpay widget with post_data:\n%s", pprint.pformat(post_data))

        # تحقّق حدّ أدنى من الحقول
        required = ['payment_url', 'checkout_id', 'formatted_amount', 'paymentMethodCode']
        missing = [k for k in required if not post_data.get(k)]
        if missing:
            _logger.error("Missing required rendering values: %s", missing)
            return request.not_found()

        provider_code = (post_data.get('paymentMethodCode') or 'hyperpay').strip().lower()
        if provider_code not in ('hyperpay', 'mada'):
            provider_code = 'hyperpay'  # fallback آمن

        # ابنِ return_url مطلق (absolute) لتفادي أي مشاكل إعادة توجيه
        base = request.httprequest.host_url  # مثال: https://www.artcontracting.com/
        if provider_code == 'mada':
            return_url = urls.url_join(base, self._return_url_mada)
            brands = 'MADA'
        else:
            return_url = urls.url_join(base, self._return_url)
            brands = 'VISA MASTER'

        form_values = {
            'payment_url': post_data.get('payment_url'),
            'checkout_id': post_data.get('checkout_id'),
            'amount': post_data.get('formatted_amount'),
            'provider': provider_code,  # يستخدمه القالب في إبراز الشعارات
            'return_url': return_url,
            'brands': brands,
        }

        _logger.info("rendering ds_payment_hyperpay.hyperpay_payment_form with values:\n%s",
                     pprint.pformat(form_values))
        return request.render('ds_payment_hyperpay.hyperpay_payment_form', form_values)
