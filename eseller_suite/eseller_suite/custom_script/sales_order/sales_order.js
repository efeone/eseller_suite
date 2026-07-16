frappe.ui.form.on('Sales Order', {
	refresh(frm) {
		if (frm.doc.amazon_order_id) {
			frm.disable_form();
			frm.disable_save();
			if (!frappe.user.has_role('System Manager')) {
				$('.custom-actions').hide()
			}
		}
		handle_multi_company_btn(frm);
	}
});

function handle_multi_company_btn(frm) {
	if (frm.doc.amazon_order_id && frm.doc.has_multi_company_exception) {
		frm.page.add_menu_item(__('Split Sales Order based on Company'), function () {
			split_so_based_on_company(frm);
		});
	}
}

function split_so_based_on_company(frm) {
	frappe.call({
		method: 'eseller_suite.eseller_suite.custom_script.sales_order.sales_order.split_so_based_on_company',
		args: {
			sales_order: frm.doc.name
		},
		callback: function (r) {
			if (r.message) {
				frappe.set_route('List', 'Sales Order', {
					amazon_order_id: frm.doc.amazon_order_id
				});
			}
		}
	});
}
