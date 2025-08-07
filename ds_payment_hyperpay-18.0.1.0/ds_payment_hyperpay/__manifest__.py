{
    'name': "HyperPay Payment Gateway",
    'version': '18.0.1.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 51,
    'summary': 'Integration with HyperPay payment gateway for MADA, Visa and MasterCard payments',
    'description': """
        Complete integration with HyperPay payment gateway
        Supports MADA, Visa and MasterCard payments
        Designed for Saudi Arabian market
    """,
    'author': 'DevSouls',
    'support': "odoodevsouls@gmail.com",
    'website': "https://www.dev-souls.com",
    'images': ['static/description/banner.png'],
    'depends': ['payment', 'website_sale'],
    'data': [
        'data/payment_provider_data.xml',
        'views/payment_provider_views.xml',
        'views/payment_hyperpay_templates.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'ds_payment_hyperpay/static/src/js/payment_form.js',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'license': 'OPL-1',
    'installable': True,
    'application': True,
    'auto_install': False,
    'price': 95.0,
    'currency': 'USD'
}
