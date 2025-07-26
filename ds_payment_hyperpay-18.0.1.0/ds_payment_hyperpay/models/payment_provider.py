# -*- coding: utf-8 -*-
#############################################################################
#
#    Copyright (C) 2024-TODAY
#    Author: Odoo DevSouls <odoodevsouls@gmailcom>
#
#############################################################################
"""
TEST CARDS
-----------
VISA: 4111111111111111
MASTER: 5212345678901234
MADA: 4464043505991993
"""

import json
import logging

from odoo import api, fields, models, _

from urllib.parse import urlencode
from urllib.request import build_opener, Request, HTTPHandler
from urllib.error import HTTPError, URLError

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(selection_add=[('hyperpay', "HyperPay")], ondelete={'hyperpay': 'set default'})
    hyperpay_merchant_id = fields.Char(string='Merchant/Entity ID')
    hyperpay_merchant_id_mada = fields.Char(string='Merchant/Entity ID (MADA)')
    hyperpay_secret_key = fields.Char(string='Secret Key', required_if_provider='hyperpay')

    def get_hyperpay_urls(self):
        if self.state == 'enabled':
            return {
                'hyperpay_form_url': 'https://eu-prod.oppwa.com/v1/checkouts',
                'hyperpay_process_url': 'https://eu-prod.oppwa.com/',
            }
        else:
            return {
                'hyperpay_form_url': 'https://eu-test.oppwa.com/v1/checkouts',
                'hyperpay_process_url': 'https://eu-test.oppwa.com/',
            }

    def _hyperpay_make_request(self, data):
        self.ensure_one()
        try:
            url = self.get_hyperpay_urls()['hyperpay_form_url']
            opener = build_opener(HTTPHandler)
            request = Request(url, data=urlencode(data).encode('utf-8'))
            request.add_header('Authorization', 'Bearer %s' % self.hyperpay_secret_key)
            request.get_method = lambda: 'POST'
            response = opener.open(request)
            return json.loads(response.read())
        except HTTPError as e:
            return json.loads(e.read())
        except URLError as e:
            return e.reason

    def _hyperpay_get_payment_status(self, url, provider_code):
        merchant_id = self.hyperpay_merchant_id_mada if provider_code == 'mada' else self.hyperpay_merchant_id
        url += '?entityId=%s' % merchant_id
        try:
            opener = build_opener(HTTPHandler)
            request = Request(url, data=b'')
            request.add_header('Authorization', 'Bearer %s' % self.hyperpay_secret_key)
            request.get_method = lambda: 'GET'
            response = opener.open(request)
            return json.loads(response.read())
        except HTTPError as e:
            return json.loads(e.read())
        except URLError as e:
            return e.reason
