# -*- coding: utf-8 -*-
#############################################################################
#
#    Copyright (C) 2024-TODAY
#    Author: Odoo DevSouls <odoodevsouls@gmailcom>
#
#############################################################################
"""
HyperPay Payment Gateway Integration

Test Cards:
-----------
VISA: 4111111111111111
MASTER: 5212345678901234
MADA: 4464043505991993
"""

import json
import logging
from urllib.parse import urlencode
from urllib.request import build_opener, Request, HTTPHandler
from urllib.error import HTTPError, URLError

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('hyperpay', "HyperPay")],
        ondelete={'hyperpay': 'set default'}
    )
    
    hyperpay_merchant_id = fields.Char(
        string='Merchant ID (Visa/MasterCard)',
        required_if_provider='hyperpay',
        help='Entity ID provided by HyperPay for Visa/MasterCard transactions'
    )
    
    hyperpay_merchant_id_mada = fields.Char(
        string='Merchant ID (MADA)',
        help='Entity ID provided by HyperPay for MADA transactions'
    )
    
    hyperpay_secret_key = fields.Char(
        string='Access Token',
        required_if_provider='hyperpay',
        help='Secret key provided by HyperPay'
    )

    def get_hyperpay_urls(self):
        """Return HyperPay API endpoints based on provider state"""
        base_urls = {
            'enabled': {
                'form_url': 'https://eu-prod.oppwa.com/v1/checkouts',
                'process_url': 'https://eu-prod.oppwa.com/',
            },
            'test': {
                'form_url': 'https://eu-test.oppwa.com/v1/checkouts',
                'process_url': 'https://eu-test.oppwa.com/',
            }
        }
        return base_urls.get(self.state, base_urls['test'])

    def _hyperpay_make_request(self, data):
        """Make API request to HyperPay"""
        self.ensure_one()
        try:
            url = self.get_hyperpay_urls()['form_url']
            opener = build_opener(HTTPHandler)
            request = Request(url, data=urlencode(data).encode('utf-8'))
            request.add_header('Authorization', f'Bearer {self.hyperpay_secret_key}')
            request.add_header('Content-Type', 'application/x-www-form-urlencoded')
            response = opener.open(request)
            return json.loads(response.read().decode('utf-8'))
        except HTTPError as e:
            _logger.error("HyperPay API Error: %s", e.read().decode())
            return {'result': {'code': '999.999.999', 'description': str(e)}}
        except URLError as e:
            _logger.error("HyperPay Connection Error: %s", e.reason)
            return {'result': {'code': '999.999.998', 'description': str(e.reason)}}
        except Exception as e:
            _logger.error("HyperPay Unexpected Error: %s", str(e))
            return {'result': {'code': '999.999.997', 'description': str(e)}}

    def _hyperpay_get_payment_status(self, resource_path, provider_code):
        """Check payment status with HyperPay"""
        self.ensure_one()
        try:
            merchant_id = self.hyperpay_merchant_id_mada if provider_code == 'mada' else self.hyperpay_merchant_id
            url = f"{self.get_hyperpay_urls()['process_url']}{resource_path}?entityId={merchant_id}"
            
            opener = build_opener(HTTPHandler)
            request = Request(url)
            request.add_header('Authorization', f'Bearer {self.hyperpay_secret_key}')
            response = opener.open(request)
            return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            _logger.error("HyperPay Status Check Error: %s", str(e))
            return {'result': {'code': '999.999.996', 'description': str(e)}}
