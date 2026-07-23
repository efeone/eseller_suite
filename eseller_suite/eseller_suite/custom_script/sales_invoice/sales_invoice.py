import frappe
from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_repository import create_stock_entry

def before_validate(doc, method):
	'''
		Method which trigger on before_validate event of Sales Invoice
	'''
	set_discount_based_on_amazon_value(doc)

def validate(doc, method):
	if not doc.is_return and doc.update_stock:
		for item in doc.items:
			if frappe.db.get_value("Item", item.item_code, "has_serial_no"):
				item.use_serial_batch_fields = 1
				serial_nos = get_serial_nos(item.warehouse, item.item_code, item.qty)
				item.serial_no = "\n".join(serial_nos)

def after_insert(doc, method):
	'''
		Method which get trgiggered in after_insert event
	'''
	delete_failed_sync_records(doc)

def before_submit(doc, method):
	'''
		Method which get trgiggered in before_submit event
	'''
	pass
	# if doc.replaced_order_id and doc.amazon_order_id:
	# 	create_stock_entry(doc.name)

def on_submit(doc, method):
	'''
		Method which get trgiggered in on_submit event
	'''
	if doc.amazon_invoice_id:
		unset_stn_exception(doc)
	delete_failed_invoice_records(doc)

def on_cancel(doc, method):
	'''
		Method which get trgiggered in on_cancel event
	'''
	doc.ignore_linked_doctypes = (
		"GL Entry",
		"Stock Ledger Entry",
		"Repost Item Valuation",
		"Repost Payment Ledger",
		"Repost Payment Ledger Items",
		"Repost Accounting Ledger",
		"Repost Accounting Ledger Items",
		"Unreconcile Payment",
		"Unreconcile Payment Entries",
		"Payment Ledger Entry",
		"Serial and Batch Bundle",
		"Amazon STN Entry",
	)
	if doc.is_return:
		for item in doc.items:
			if item.sales_invoice_item and frappe.db.exists('Sales Invoice Item', item.sales_invoice_item):
				frappe.db.set_value('Sales Invoice Item', item.sales_invoice_item, 'refunded', 0)

def on_trash(doc, method):
	delete_failed_invoice_records(doc)

def get_serial_nos(warehouse, item_code, qty):
	"""
		Fetch serial numbers using FIFO for the given item and quantity.
	"""
	serial_no_list = frappe.get_all("Serial No",
		filters={
			"warehouse": warehouse,
			"item_code": item_code,
			"status": "Active"
		},
		fields=["name"],
		order_by="creation asc",
		limit=qty
	)
	if len(serial_no_list) < qty:
		frappe.throw(f"Not enough serial numbers available for item {item_code}.")
	return [serial_no.name for serial_no in serial_no_list]

def set_discount_based_on_amazon_value(doc):
	'''
		Method to set dicount based on Amazon value and outstanding amount
	'''
	if doc.amazon_invoice_value and doc.outstanding_amount:
		diff = doc.outstanding_amount - doc.amazon_invoice_value
		# Add discount if difference is between -1 to 1, else it may be some error
		if diff and (1 > diff > -1):
			doc.apply_discount_on = 'Grand Total'
			doc.discount_amount = diff

def unset_stn_exception(doc):
	'''
		Method to unset STN exception after Sales Invoice submission
	'''
	stn_items = frappe.get_all('Amazon STN Entry Item', filters={'sales_invoice': doc.name}, fields=['name', 'purchase_invoice'])

	if not stn_items:
		return

	pi_ref = stn_items[0].purchase_invoice

	if pi_ref and frappe.db.get_value('Purchase Invoice', pi_ref, 'docstatus') == 1:
		for item in stn_items:
			frappe.db.set_value(
				'Amazon STN Entry Item', item.name, 'error_log', '', update_modified=False
			)

def delete_failed_invoice_records(self):
	'''
		Method to delete failed invoice records related to the invoice
	'''
	frappe.db.delete(
		"Amazon Failed Invoice Record",
		{
			"invoice_id": self.name
		}
	)

def delete_failed_sync_records(self):
	'''
		Method to delete failed invoice records related to the invoice
	'''
	filters = {
		"amazon_order_id": self.amazon_order_id,
	}
	if self.grand_total == 0:
		filters['replaced_jv'] = ['is', 'set']
	frappe.db.delete( "Amazon Failed Sync Record", filters=filters)
