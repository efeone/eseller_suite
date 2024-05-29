// Copyright (c) 2024, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("Amazon Payment Extractor", {
	refresh: function(frm) {
    if (!frm.doc.sales_invoice_created) {
      frm.add_custom_button('Create Sales Invoice', () => {
        frappe.call({
          method: 'eseller_suite.eseller_suite.doctype.amazon_payment_extractor.amazon_payment_extractor.create_sales_invoices',
          args: {
            docname: frm.doc.name
          },
          freeze: true,
          callback: (r) => {
            frm.reload_doc();
          }
        });
      });
    }
		else if(!frm.doc.amazon_easy_shipping_charge){
			frm.add_custom_button('Amazon Easy Shipping Charge', () => {
				frappe.call({
          method: 'eseller_suite.eseller_suite.doctype.amazon_payment_extractor.amazon_payment_extractor.update_sales_invoices',
          args: {
            docname: frm.doc.name
          },
          freeze: true,
          callback: (r) => {
            frappe.msgprint(r.message);
            frm.reload_doc();
          }
        });
      });
    }
		else if (!frm.doc.refund){
			frm.add_custom_button('Refund', () => {
				frappe.call({
          method: 'eseller_suite.eseller_suite.doctype.amazon_payment_extractor.amazon_payment_extractor.update_sales_invoices_refund',
          args: {
            docname: frm.doc.name
          },
          freeze: true,
          callback: (r) => {
            frappe.msgprint(r.message);
            frm.reload_doc();
          }
        });
      });
    }
		else if (!frm.doc.fulfillment_fee_refund){
			frm.add_custom_button('Fulfillment Fee Refund', () => {
				frappe.call({
          method: 'eseller_suite.eseller_suite.doctype.amazon_payment_extractor.amazon_payment_extractor.update_sales_invoices_fulfillment_fee_refund',
          args: {
            docname: frm.doc.name
          },
          freeze: true,
          callback: (r) => {
            frappe.msgprint(r.message);
            frm.reload_doc();
          }
        });
      });
    }
	}
});
