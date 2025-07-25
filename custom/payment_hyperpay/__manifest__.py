{
    'name': 'HyperPay Payment Gateway',
    'version': '1.1',
    'category': 'Accounting/Payment',
    'summary': 'HyperPay Payment Integration',
    'description': 'Integration of HyperPay payment gateway for Odoo v18',
    'author': 'Your Name',
    'depends': ['payment', 'website_sale'],  # أضف website_sale إذا كنت تستخدمه
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
    'application': False,  # غيرها إلى False لتجنب مشاكل التحميل
    'auto_install': False,
    'license': 'LGPL-3',
}