// Copyright (c) 2024, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("eSeller Settings", {
	refresh(frm) {
		set_filters(frm);
		set_sales_invoice_series(frm);
        set_journal_entry_series(frm);
		set_custom_buttons(frm);
	},
	allow_missing_warehouse_creation(frm) {
		if (frm.doc.allow_missing_warehouse_creation) {
			populate_parent_warehouses(frm);
		} else {
			clear_parent_warehouses(frm);
		}
	}
});

frappe.ui.form.on("Amazon Payment Account", {
	payment_accounts_add(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, 'company', '');
		frappe.model.set_value(cdt, cdn, 'mode_of_payment', frm.doc.default_mode_of_payment);
		frappe.model.set_value(cdt, cdn, 'use_reserve_lines_in_amazon_payment_entry', frm.doc.use_reserve_lines_in_amazon_payment_entry);
	},
});

frappe.ui.form.on("eSeller Parent Warehouse", {
	parent_warehouses_add(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, 'company', '');
	},
});

function set_filters(frm) {
	frm.set_query("main_warehouse", () => {
		return {
			filters: {
				"is_group": 0
			}
		};
	});
	frm.set_query("inter_company_price_list", () => {
		return {
			filters: {
				"buying": 1,
				"selling": 1,
			}
		};
	});
	frm.set_query('default_parent_warehouse', 'parent_warehouses', (doc, cdt, cdn) => {
		let row = locals[cdt][cdn];
		return {
			filters: {
				company: row.company || "",
				is_group: 1
			}
		};
	});
	default_account_filters(frm);
}

/**
 * Populate parent warehouses via server call Clears parent warehouses when checkbox is unchecked
 */
function populate_parent_warehouses(frm) {
	frappe.call({
		method: "eseller_suite.eseller_suite.doctype.eseller_settings.eseller_settings.get_all_companies",
		callback: function (r) {
			if (r.message) {
				frm.clear_table("parent_warehouses");

				r.message.forEach(comp => {
					let row = frm.add_child("parent_warehouses");
					row.company = comp.name;
				});

				frm.refresh_field("parent_warehouses");
			}
		}
	});
}

/*
 * Clear table when checkbox unchecked
 */
function clear_parent_warehouses(frm) {
	frm.clear_table("parent_warehouses");
	frm.refresh_field("parent_warehouses");
}

function default_account_filters(frm) {
	const account_fields = [
		'inventory_reimbursement_income_account',
		'other_income_account',
		'amazon_reserve_fund_account',
		'reimbursement_income_account',
		'inventory_reimbursement_account',
		'other_expenses_account',
		'order_cancellation_account',
		'amazon_reserve_income_account',
		'amazon_reserve_expense_account',
		'shipping_charges_account',
		'fulfilment_fee_refund_account',
		'tds_reimbursement_account',
		'fba_flex_weight_account',
        'inter_company_income_account'
	];

	account_fields.forEach(fieldname => {
		frm.set_query(fieldname, 'payment_accounts', (doc, cdt, cdn) => {
			let row = locals[cdt][cdn];
			return {
				filters: {
					company: row.company || "",
					is_group: 0
				}
			};
		});
	});
}

function set_sales_invoice_series(frm) {
	frappe.call({
		method: 'eseller_suite.eseller_suite.doctype.eseller_settings.eseller_settings.get_naming_series_options',
		args: { doctype: 'Sales Invoice' },
	}).then(r => {
		frm.set_df_property('stn_sales_invoice_series', 'options', (r.message || []).join("\n"));
	});
}

function set_journal_entry_series(frm) {
	frappe.call({
		method: 'eseller_suite.eseller_suite.doctype.eseller_settings.eseller_settings.get_naming_series_options',
		args: { doctype: 'Journal Entry' },
	}).then(r => {
		frm.set_df_property('journal_entry_series', 'options', (r.message || []).join("\n"));
	});
}

function set_custom_buttons(frm) {
	if (!frappe.user.has_role('System Manager')) {
		return
	}

	const patch_buttons = [
		{
			label: 'Update SI Items',
			method: 'eseller_suite.eseller_suite.utils.update_missing_items_in_sales_invoices',
			freeze_message: __('Updating SI Items...'),
		},
		{
			label: 'Delete Invoices with -ve stock',
			method: 'eseller_suite.eseller_suite.utils.delete_submitted_invoices_without_stock',
			freeze_message: __('Deleting Invoices with negative stock...'),
		},
		{
			label: 'Delete Invoices without GLE',
			method: 'eseller_suite.eseller_suite.utils.delete_submitted_invoices_without_gle',
			freeze_message: __('Deleting Invoices without GLE...'),
		},
		{
			label: 'Delete SO without SI',
			method: 'eseller_suite.eseller_suite.utils.delete_submitted_so_without_si',
			freeze_message: __('Deleting SO without SI...'),
		},
	];

	patch_buttons.forEach(({ label, method, freeze_message }) => {
		frm.add_custom_button(__(label), () => {
			frappe.call({
				method,
				freeze: true,
				freeze_message,
			}).then(() => frappe.msgprint(__('Successful')))
				.catch(() => frappe.msgprint({ message: __('Operation failed.'), indicator: 'red' }));
		}, __('Patches'));
	});
}
