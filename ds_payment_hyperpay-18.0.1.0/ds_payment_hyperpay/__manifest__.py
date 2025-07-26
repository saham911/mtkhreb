# -*- coding: utf-8 -*-
#############################################################################
#
#    Copyright (C) 2024-TODAY
#    Author: Odoo DevSouls <odoodevsouls@gmailcom>
#
#############################################################################
{
    'name': "Hyperpay Payment Gateway",
    'version': '18.0.0.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 51,
    'summary': 'Hyperpay Payment Gateway to intergrate Mada Card, Visa Card and Master Card payments. It supports Credit cards, Debit Cards and MADA Card payments in KSA, Saudi Arabia',
    'author': 'DevSouls',
    'support': "odoodevsouls@gmailcom",
    'images': ['static/description/banner.png'],
    'description': "Hyperpay Payment Gateway (Mada/Visa/Master), MADA Payments in KSA",
    'depends': ['payment', 'website_sale'],
    'data': [
        'views/payment_provider_views.xml',
        'views/payment_hyperpay_templates.xml',
        'data/payment_provider_data.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'license': 'OPL-1',
    'installable': True,
    'auto_install': False,
    'application': False,
    'price': 95.0,
    'currency': 'USD'
}
