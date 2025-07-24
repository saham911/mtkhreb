odoo.define('artcontracting.hyperpay', function (require) {
    'use strict';

    var ajax = require('web.ajax');
    var core = require('web.core');
    var _t = core._t;

    function loadHyperPayScript() {
        var script = document.createElement('script');
        script.src = 'https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=' + checkoutId;
        script.setAttribute('data-brands', 'VISA MASTER MADA');
        document.head.appendChild(script);
    }

    function initHyperPayForm() {
        var $form = $('#hyperpay_payment_form');
        var acquirer_id = $form.find('input[name="acquirer_id"]').val();
        
        ajax.jsonRpc('/hyperpay/payment', 'call', {
            'acquirer_id': acquirer_id,
        }).then(function (result) {
            if (result.id) {
                var checkoutId = result.id;
                var script = document.createElement('script');
                script.src = 'https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=' + checkoutId;
                script.setAttribute('data-brands', 'VISA MASTER MADA');
                document.head.appendChild(script);
                
                // Add MADA script
                var madaScript = document.createElement('script');
                madaScript.src = 'https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=' + checkoutId;
                madaScript.setAttribute('data-brands', 'MADA');
                document.head.appendChild(madaScript);
                
                // Add 3DS Secure redirection script
                var wpwlScript = document.createElement('script');
                wpwlScript.type = 'text/javascript';
                wpwlScript.text = 'var wpwlOptions = { paymentTarget: "_top" }';
                document.head.appendChild(wpwlScript);
                
                // Show payment form
                $form.find('.payment-form').show();
            } else {
                console.error('Error initializing HyperPay:', result);
            }
        });
    }

    return {
        initHyperPayForm: initHyperPayForm,
    };
});