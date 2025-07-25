{
    'name': 'HyperPay Payment Gateway',
    'version': '1.1',
    'category': 'Accounting/Payment',
    'summary': 'Integrate HyperPay payment gateway with Odoo',
    'description': 'HyperPay payment gateway integration for Odoo v18',
    'author': 'Your Name',
    'depends': ['payment'],
    'data': [
        'views/payment_views.xml',
        'views/hyperpay_templates.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'payment_hyperpay/static/src/js/hyperpay.js',
        ],
    },
    'installable': True,
    'application': True,
}
