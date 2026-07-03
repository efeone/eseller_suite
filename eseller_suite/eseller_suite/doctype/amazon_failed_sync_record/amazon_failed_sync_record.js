// Copyright (c) 2024, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("Amazon Failed Sync Record", {
	refresh(frm) {
		frm.disable_save();
		frm.disable_form();
		if (!frm.is_new()) {
			handle_custom_buttons(frm);
		}
	}
});

function handle_custom_buttons(frm) {
	if (!frm.is_new()) {
		if (frm.doc.amazon_order_id && !frm.doc.replaced_order_id && !frm.doc.is_multi_company_exception && frm.doc.grand_total > 0) {
			frm.add_custom_button('Retry', () => {
				retry_fetching(frm);
			}).addClass("btn-primary");
		}
		if (frm.doc.replaced_order_id) {
			if (!frm.doc.replaced_jv) {
				frappe.db.get_value('Journal Entry', { amazon_order_id: frm.doc.amazon_order_id }, 'name')
					.then(r => { // checking if the replaced jv is already created from another source
						if (!r.message.name) {
							frm.add_custom_button('Journal Entry', () => {
								make_frappe_call(frm, 'create_replaced_jv', 'Creating Replaced Journal Entry..');
							}, 'Create');
						}
					})
			}
			if (!frm.doc.replaced_so && frm.doc.replaced_jv) {
				frappe.db.get_value('Sales Order', { amazon_order_id: frm.doc.amazon_order_id }, 'name')
					.then(r => { // checking if the replaced so is already created from another source
						if (!r.message.name) {
							frm.add_custom_button('Sales Order', () => {
								make_frappe_call(frm, 'create_replaced_so', 'Creating Replaced Sales Order..');
							}, 'Create');
						}
					})
			}
		}
		else if (frm.doc.grand_total < 0) {
			frappe.db.get_value('Journal Entry', { amazon_order_id: frm.doc.amazon_order_id }, 'name').then(r => {
				// checking if the adjustment jv is already created from another source
				if (!r.message.name) {
					frm.add_custom_button('Journal Entry', () => {
						make_frappe_call(frm, 'create_adjustment_jv', 'Creating Adjustment Journal Entry..');
					}, 'Create');
				}
				else {
					frappe.db.get_value('Sales Order', { amazon_order_id: frm.doc.amazon_order_id }, 'name').then(r => {
						// checking if the adjustment so is already created from another source
						if (!r.message.name) {
							frm.add_custom_button('Sales Order', () => {
								make_frappe_call(frm, 'create_adjustment_so', 'Creating Adjustment Sales Order..');
							}, 'Create');
						}
					})
				}
			})
		}
        // Return invoice creation
		if(frm.doc.is_return_exception){
			frappe.db.get_value('Sales Invoice', { amazon_order_id: frm.doc.amazon_order_id }, 'name').then(r => {
				// checking if the adjustment so is already created from another source
				if (!r.message.name) {
					frm.add_custom_button('Credit Note', () => {
						make_frappe_call(frm, 'create_return_invoice', 'Creating Credit Note...');
					}, 'Create');
				}
			})
		}
	}
}

function retry_fetching(frm) {
	if (frm.doc.amazon_order_id) {
		frm.call({
			method: "retry_fetching",
			doc: frm.doc,
			freeze: true,
			freeze_message: __("Syncing Sales Order.."),
			callback: (r) => {
				if (r && r.message) {
					// Check if record was deleted (success case)
					if (r.message.success) {
						frappe.show_alert({
							message: r.message.message || __('Order/Invoice created successfully. Failed sync record deleted.'),
							indicator: 'green'
						}, 5);
						// Redirect to list view since record is deleted
						setTimeout(() => {
							frappe.set_route("List", "Amazon Failed Sync Record");
						}, 1000);
					} else {
						frappe.show_alert({
							message: __('Sales Orders created/updated successfully'),
							indicator: 'green'
						}, 5);
						frm.reload_doc();
					}
				}
				else {
					frappe.show_alert({
						message: __('Failed to create/update Sales Order. Please check Amazon Failed Sync Record'),
						indicator: 'red'
					}, 5);
					frm.reload_doc();
				}
			}
		});
	}
	else {
		frappe.throw(__('Amazon Order ID is required'))
	}
}

function make_frappe_call(frm, method, msg) {
	frm.call({
		method: method,
		doc: frm.doc,
		freeze: true,
		freeze_message: __(msg),
		callback: (r) => {
			frm.reload_doc();
			if(method === 'create_return_invoice'){
				setTimeout(() => {
					frappe.set_route("List", "Amazon Failed Sync Record");
				}, 1000);
			}
		}
	});
}