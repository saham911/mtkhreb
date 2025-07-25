{
    'name': 'HyperPay Payment Gateway',
    'version': '1.2',
    'category': 'Accounting/Payment',
    'summary': 'HyperPay Payment Integration',
    'description': 'Integration of HyperPay payment gateway for Odoo v18',
    'author': 'Your Name',
    'depends': ['payment', 'website_sale'],
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
    'application': False, 
    'auto_install': False,
    'license': 'LGPL-3',
}