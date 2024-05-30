// Copyright (c) 2024, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("Flipkart Payment Extractor", {
	refresh(frm) {
    if (!frm.doc.purchase_invoice_created) {
      frm.add_custom_button('Create Purchase Invoice', () => {
        frappe.call({
          method: 'eseller_suite.eseller_suite.doctype.flipkart_payment_extractor.flipkart_payment_extractor.create_purchase_invoices',
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
	},
});
