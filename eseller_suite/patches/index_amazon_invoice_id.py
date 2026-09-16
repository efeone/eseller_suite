import frappe

DOCTYPES = ["Stock Entry", "Sales Invoice", "Purchase Invoice"]


def execute():
	"""
		Index the amazon_invoice_id Custom Field on the transaction doctypes.

		STN processing and cancellation filter on this field against tables holding
		tens of thousands of rows, once per STN row. Without an index each lookup is
		a full table scan, which is a large part of why cancelling a big STN Entry
		times out on a lock wait.
	"""
	print('Adding index on amazon_invoice_id...')

	for doctype in DOCTYPES:
		custom_field = frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": "amazon_invoice_id"})
		if not custom_field:
			continue

		frappe.db.set_value("Custom Field", custom_field, "search_index", 1, update_modified=False)
		frappe.db.add_index(doctype, ["amazon_invoice_id"])
