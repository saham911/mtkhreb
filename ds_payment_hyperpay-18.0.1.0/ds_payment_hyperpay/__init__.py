# -*- coding: utf-8 -*-
#############################################################################
#
#    Copyright (C) 2024-TODAY
#    Author: Odoo DevSouls <odoodevsouls@gmailcom>
#
#############################################################################

from . import controllers
from . import models
from . import hyperpay_utils
from odoo.addons.payment import setup_provider, reset_payment_provider


def post_init_hook(env):
    """Function to set up the payment provider 'HyperPay' after
    module installation."""
    setup_provider(env, 'hyperpay')


def uninstall_hook(env):
    """Function to reset the payment provider 'HyperPay' before module
    uninstallation."""
    reset_payment_provider(env, 'hyperpay')
