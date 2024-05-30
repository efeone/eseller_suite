// Copyright (c) 2024, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("Amazon Payment Extractor", {
	refresh: function(frm) {
		if (!frm.doc.sales_invoice_created) {
			frm.add_custom_button('Sales Invoices', () => {
				if(frm.doc.missing_items){
					frm.scroll_to_field('missing_items');
			        frappe.dom.unfreeze();
			        frappe.throw({message:__("Please map Missing Items with Product Details."), title: __("Item Mapping Missing")});
				}
				else {
					create_sales_invoices(frm);
				}
			}, 'Create');
		}
		else if(!frm.doc.amazon_easy_shipping_charge){
			frm.add_custom_button('Amazon Easy Shipping Charge', () => {
				add_amazon_shipping_charges(frm)
			}, 'Add');
		}
		else if (!frm.doc.refund){
			frm.add_custom_button('Refund/Return Invoice', () => {
				create_return_invoice(frm);
			}, 'Create');
		}
		else if (!frm.doc.fulfillment_fee_refund){
			frm.add_custom_button('Fulfillment Fee Refund', () => {
				add_fulfillment_fee_refund(frm);
			}, 'Add');
		}
		else if (!frm.doc.invoices_submitted){
			frm.add_custom_button('Submit Invoices', () => {
				submit_all_invoices(frm);
			});
		}
	}
});

function create_sales_invoices(frm){
	frappe.call({
		method: 'eseller_suite.eseller_suite.doctype.amazon_payment_extractor.amazon_payment_extractor.create_sales_invoices',
		args: {
			docname: frm.doc.name
		},
		freeze: true,
		freeze_message: __("Creating Sales Invoices..."),
		callback: (r) => {
			frm.reload_doc();
		}
	});
}

function add_amazon_shipping_charges(frm){
	frappe.call({
		method: 'eseller_suite.eseller_suite.doctype.amazon_payment_extractor.amazon_payment_extractor.update_sales_invoices_with_shipping_charges',
		args: {
			docname: frm.doc.name
		},
		freeze: true,
		freeze_message: __("Adding Amazon Easy Shipping Charges..."),
		callback: (r) => {
			frm.reload_doc();
		}
	});
}

function create_return_invoice(frm){
	frappe.call({
		method: 'eseller_suite.eseller_suite.doctype.amazon_payment_extractor.amazon_payment_extractor.create_return_invoice',
		args: {
			docname: frm.doc.name
		},
		freeze: true,
		freeze_message: __("Creating Return Invoices..."),
		callback: (r) => {
			frm.reload_doc();
		}
	});
}

function add_fulfillment_fee_refund(frm){
	frappe.call({
		method: 'eseller_suite.eseller_suite.doctype.amazon_payment_extractor.amazon_payment_extractor.update_sales_invoices_with_fulfillment_fee_refund',
		args: {
			docname: frm.doc.name
		},
		freeze: true,
		freeze_message: __("Adding Fulfillment Fee Refund..."),
		callback: (r) => {
			frm.reload_doc();
		}
	});
}

function submit_all_invoices(frm){
	frappe.call({
		method: 'eseller_suite.eseller_suite.doctype.amazon_payment_extractor.amazon_payment_extractor.submit_all_invoices',
		args: {
			docname: frm.doc.name
		},
		freeze: true,
		freeze_message: __("Submiting Invoices..."),
		callback: (r) => {
			frm.reload_doc();
		}
	});
}
