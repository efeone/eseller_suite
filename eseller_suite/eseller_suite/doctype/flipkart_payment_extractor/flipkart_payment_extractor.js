// Copyright (c) 2024, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("Flipkart Payment Extractor", {
	refresh(frm) {
    if (!frm.doc.purchase_invoice_created && !frm.doc.flipkart_order) {
      flipkart_ads(frm)
			flipkart_order(frm)
    }
		else if(!frm.doc.flipkart_order && frm.doc.purchase_invoice_created){
			flipkart_order(frm)
		}
		else if(frm.doc.flipkart_order && !frm.doc.purchase_invoice_created){
			flipkart_ads(frm)
		}
	},
});

function flipkart_ads(frm){
	frm.add_custom_button('Flipkart Ads Purchase Invoice', () => {
		frappe.call({
			method: 'eseller_suite.eseller_suite.doctype.flipkart_payment_extractor.flipkart_payment_extractor.create_ads_purchase_invoices',
			args: {
				docname: frm.doc.name
			},
			freeze: true,
			callback: (r) => {
				frm.reload_doc();
			}
		});
	}, 'Create');
}

function flipkart_order(frm){
	frm.add_custom_button('Flipkart Order Purchase Invoice', () => {
		frappe.call({
			method: 'eseller_suite.eseller_suite.doctype.flipkart_payment_extractor.flipkart_payment_extractor.create_order_purchase_invoices',
			args: {
				docname: frm.doc.name
			},
			freeze: true,
			callback: (r) => {
				frm.reload_doc();
			}
		});
	}, 'Create');
}
