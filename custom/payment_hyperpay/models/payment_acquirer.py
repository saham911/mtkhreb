from odoo import models, fields, api

class PaymentAcquirer(models.Model):
    _inherit = 'payment.acquirer'

    provider = fields.Selection(
        selection_add=[('hyperpay', 'HyperPay')],
        ondelete={'hyperpay': 'set default'}
    )
    
    hyperpay_access_token = fields.Char(
        string='Access Token',
        required_if_provider='hyperpay',
        groups='base.group_user'
    )
    
    hyperpay_entity_id = fields.Char(
        string='Entity ID (Visa/Master)',
        required_if_provider='hyperpay',
        groups='base.group_user'
    )
    
    hyperpay_mada_entity_id = fields.Char(
        string='Entity ID (MADA)',
        required_if_provider='hyperpay',
        groups='base.group_user'
    )

    @api.model
    def _get_compatible_acquirers(self, *args, **kwargs):
        """ Override to add specific domain for HyperPay """
        acquirers = super()._get_compatible_acquirers(*args, **kwargs)
        return acquirers