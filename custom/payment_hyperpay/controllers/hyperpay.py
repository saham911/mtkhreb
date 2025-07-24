from odoo import http
from odoo.http import request
import json
import requests

class HyperPayController(http.Controller):
    @http.route('/hyperpay/payment', type='json', auth='public', website=True)
    def hyperpay_payment(self, **post):
        acquirer = request.env['payment.acquirer'].browse(int(post.get('acquirer_id')))
        order = request.website.sale_get_order()
        
        base_url = request.env['ir.config_parameter'].get_param('web.base.url')
        return_url = f"{base_url}/hyperpay/return"
        
        # Prepare payment data
        amount = order.amount_total
        currency = order.currency_id.name
        customer_email = order.partner_id.email
        merchantTransactionId = order.name
        
        # Prepare billing address
        billing_address = {
            'street1': order.partner_id.street or '',
            'city': order.partner_id.city or '',
            'state': order.partner_id.state_id.name or '',
            'country': order.partner_id.country_id.code or '',
            'postcode': order.partner_id.zip or '',
        }
        
        # Prepare payment payload
        payload = {
            'entityId': acquirer.hyperpay_entity_id,
            'amount': str(amount),
            'currency': currency,
            'paymentType': 'DB',
            'merchantTransactionId': merchantTransactionId,
            'customer.email': customer_email,
            'billing.street1': billing_address['street1'],
            'billing.city': billing_address['city'],
            'billing.state': billing_address['state'],
            'billing.country': billing_address['country'],
            'billing.postcode': billing_address['postcode'],
            'customer.givenName': order.partner_id.name.split(' ')[0],
            'customer.surname': ' '.join(order.partner_id.name.split(' ')[1:]),
            'testMode': 'EXTERNAL',
            'customParameters[3DS2_enrolled]': 'true',
            'notificationUrl': f"{base_url}/hyperpay/webhook",
        }
        
        # Make request to HyperPay
        headers = {
            'Authorization': f'Bearer {acquirer.hyperpay_access_token}',
        }
        
        response = requests.post(
            'https://eu-test.oppwa.com/v1/checkouts',
            data=payload,
            headers=headers
        )
        
        return response.json()
    
    @http.route('/hyperpay/return', type='http', auth='public', website=True)
    def hyperpay_return(self, **post):
        # Handle return from HyperPay
        order = request.website.sale_get_order()
        if order:
            if post.get('result')['code'] == '000.100.110':
                order.action_confirm()
                return request.redirect('/shop/confirmation')
            else:
                return request.redirect('/shop/payment')
        return request.redirect('/shop')
    
    @http.route('/hyperpay/webhook', type='http', auth='public', methods=['POST'])
    def hyperpay_webhook(self, **post):
        # Handle webhook notifications
        return 'OK'