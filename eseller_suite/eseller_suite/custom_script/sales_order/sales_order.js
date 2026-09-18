/**
 *	Fixes `Invalid "depends_on" expression` when opening an Amazon order whose
 *	previous document was deleted server-side by the multi-company split flow.
 *
 *	Form.refresh(docname) calls switch_doc(docname) before assigning `this.doc`.
 *	switch_doc re-renders every grid through grid_pagination.go_to_page(1, true),
 *	so rows render while `this.doc` still points at the previous document. Once the
 *	split flow deletes that document, the realtime doc_update triggers reload_doc(),
 *	its refetch returns empty, and `this.doc` is left undefined.
 *
 *	GridRow then reads `parent = this.frm ? this.frm.doc : this.doc || null`, gets
 *	undefined, and the stock Sales Order Item expression
 *	`eval: !parent.skip_delivery_note` throws. The same stale `this.doc` also breaks
 *	Grid.get_data().
 *
 *	This lives here rather than in an app_include_js bundle on purpose: hooks are
 *	cached in production (frappe.get_hooks reads the `app_hooks` key when
 *	developer_mode is off), so a new app_include_js needs a cache clear before it
 *	loads. doctype_js for Sales Order is already registered, so this ships with the
 *	code. GridRow is an ES module default export and cannot be patched from an app.
 */
frappe.provide("eseller_suite");

eseller_suite.patch_switch_doc = function () {
	const form = frappe.ui.form.Form;
	if (!form || typeof form.prototype.switch_doc !== "function") {
		// Upstream shape changed - leave the framework alone.
		return;
	}
	if (form.prototype.switch_doc.__eseller_patched) {
		return;
	}

	const switch_doc = form.prototype.switch_doc;

	form.prototype.switch_doc = function (docname) {
		// Point the form at the incoming document before the grids re-render.
		const incoming = frappe.get_doc(this.doctype, docname);
		if (incoming) {
			this.docname = docname;
			this.doc = incoming;
		}
		return switch_doc.call(this, docname);
	};

	form.prototype.switch_doc.__eseller_patched = true;
};

eseller_suite.patch_switch_doc();

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
