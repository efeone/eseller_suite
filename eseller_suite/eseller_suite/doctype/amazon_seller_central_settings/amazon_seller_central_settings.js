// Copyright (c) 2024, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("Amazon Seller Central Settings", {
	refresh(frm) {
		frm.add_custom_button(__('Get Orders'), function(){
			frappe.call({
				method: 'eseller_suite.eseller_suite.doctype.amazon_seller_central_settings.amazon_seller_central_settings.get_orders',
				args: {
						docname: frm.doc.name
				},
				freeze: true,
				callback: (r) => {
						frm.reload_doc();
				}
			});
		});
	},
});
