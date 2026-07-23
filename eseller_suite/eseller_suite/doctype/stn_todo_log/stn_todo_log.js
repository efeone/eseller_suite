// Copyright (c) 2026, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("STN ToDo Log", {
	refresh(frm) {
		set_filters(frm);
	},
});

function set_filters(frm) {
	frm.set_query('credit_note', () => {
		return {
			filters: {
				'return_against': frm.doc.sales_invoice,
				'is_return': 1,
				'docstatus': 1
			}
		}
	});

	frm.set_query('debit_note', () => {
		return {
			filters: {
				'return_against': frm.doc.purchase_invoice,
				'is_return': 1,
				'docstatus': 1
			}
		}
	});
}