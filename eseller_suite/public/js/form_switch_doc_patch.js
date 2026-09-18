/**
 *	Fixes `Invalid "depends_on" expression` when opening an Amazon order whose
 *	previous document was deleted server-side (the multi-company split flow).
 *
 *	frappe.ui.form.Form.refresh(docname) calls switch_doc(docname) first, and only
 *	assigns `this.doc` afterwards. switch_doc re-renders every grid via
 *	grid_pagination.go_to_page(1, true), so those rows render while `this.doc` still
 *	holds the previous document. When the split flow deletes that document, the
 *	realtime doc_update triggers reload_doc(), its refetch comes back empty, and
 *	`this.doc` is left undefined.
 *
 *	GridRow then computes `parent = this.frm ? this.frm.doc : this.doc || null`,
 *	gets undefined, and the stock Sales Order Item expression
 *	`eval: !parent.skip_delivery_note` throws - which Frappe reports as an invalid
 *	depends_on expression. The same stale `this.doc` breaks Grid.get_data().
 *
 *	Pointing the form at the incoming document before the grids re-render removes
 *	the stale window. GridRow is an ES module default export and cannot be patched
 *	from an app, so this is the reachable fix.
 */
frappe.provide("frappe.ui.form");

(() => {
	const form = frappe.ui.form.Form;
	if (!form || typeof form.prototype.switch_doc !== "function") {
		// Upstream shape changed - leave the framework alone.
		return;
	}

	const switch_doc = form.prototype.switch_doc;

	form.prototype.switch_doc = function (docname) {
		const incoming = frappe.get_doc(this.doctype, docname);
		if (incoming) {
			this.docname = docname;
			this.doc = incoming;
		}
		return switch_doc.call(this, docname);
	};
})();
