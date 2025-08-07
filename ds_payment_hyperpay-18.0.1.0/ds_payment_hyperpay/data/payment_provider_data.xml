<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="payment_method_hyperpay" model="payment.method">
        <field name="name">HyperPay (VISA/Master Card)</field>
        <field name="code">hyperpay</field>
        <field name="sequence">4</field>
         <field name="image" type="base64" file="ds_payment_hyperpay/static/description/visa.png"/>
        <field name="support_tokenization">False</field>
        <field name="support_express_checkout">False</field>
        <field name="support_refund">none</field>
        <field name="supported_currency_ids"
               eval="[Command.set([
                         ref('base.EUR'),ref('base.USD'),ref('base.SAR'),
                     ])]"
        />
    </record>

    <record id="payment_method_hyperpay_mada" model="payment.method">
        <field name="name">HyperPay MADA</field>
        <field name="code">mada</field>
        <field name="sequence">5</field>
         <field name="image" type="base64" file="ds_payment_hyperpay/static/description/mada.png"/>
        <field name="support_tokenization">False</field>
        <field name="support_express_checkout">False</field>
        <field name="support_refund">none</field>
        <field name="supported_currency_ids"
               eval="[Command.set([
                         ref('base.SAR'),
                     ])]"
        />
    </record>

    <record id="payment_provider_hyperpay" model="payment.provider">
        <field name="name">HyperPay</field>
        <field name="code">hyperpay</field>
        <field name="image_128" type="base64" file="ds_payment_hyperpay/static/description/icon.png"/>
        <field name="module_id" ref="base.module_ds_payment_hyperpay"/>
        <field name="redirect_form_view_id" ref="ds_payment_hyperpay.hyperpay_redirect_form"/>
        <field name="payment_method_ids"
               eval="[Command.set([
                         ref('ds_payment_hyperpay.payment_method_hyperpay'),
                         ref('ds_payment_hyperpay.payment_method_hyperpay_mada')
                     ])]"
        />
        <field name="pre_msg">You will be redirected to HyperPay payment page</field>
    </record>
</odoo>
