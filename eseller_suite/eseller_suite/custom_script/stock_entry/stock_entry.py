import json

import frappe
from frappe.model import _
from frappe.model.base_document import flt


def validate(doc, method):
	'''
		Method which trigger on validate event of Stock Entry
	'''
	set_bundle_diff_amount(doc)

def on_submit(doc, method):
	'''
		Method which trigger on on_submit event of Stock Entry
	'''
	validate_bundle_amount_difference(doc)
	unset_stn_exception(doc)

def transfer_barcodes(doc, method=None):
	"""method transfers the barcodes on submit

	Args:
		doc (_type_): _description_
		method (_type_, optional): _description_. Defaults to None.
	"""
	for item in doc.items:
		if not item.barcode_no:
			continue

		barcodes = item.barcode_no.split("\n")
		for barcode in barcodes:
			existing_serial_no = frappe.db.exists(
				"eSeller Serial No",
				{"serial_no": barcode},
			)
			if existing_serial_no:
				serial_doc = frappe.get_doc("eSeller Serial No", existing_serial_no)
				serial_doc.status = "Transferred"
				serial_doc.warehouse = item.t_warehouse
				serial_doc.transfer_document_no = doc.name
				serial_doc.save()
	frappe.msgprint(
		"Serial Numbers are now Transferred",
		alert=True,
	)

def before_insert_custom(doc, method=None):
	"""method corrects warehouse in stock returns from returns & replaced orders if temporary stock transfer is on"""
	if doc.from_return_invoice:
		amz_settings = frappe.get_last_doc("Amazon SP API Settings")
		if amz_settings.temporary_stock_transfer_required:
			si = doc.sales_invoice_no
			return_warehouse = amz_settings.warehouse
			si_fulfillment_channel = frappe.db.get_value("Sales Invoice", si, "fulfillment_channel")

			if si_fulfillment_channel:
				if si_fulfillment_channel == 'AFN':
					return_warehouse = amz_settings.afn_warehouse

			doc.to_warehouse = return_warehouse
			for row in doc.items:
				row.t_warehouse = return_warehouse

def on_cancel(doc, method):
	doc.ignore_linked_doctypes = (
		"GL Entry",
		"Stock Ledger Entry",
		"Repost Item Valuation",
		"Serial and Batch Bundle",
		"Amazon STN Entry",
		# get_linked_docs() matches child tables on the child doctype, not the parent
		"Amazon STN Entry Item",
	)

def set_bundle_diff_amount(doc):
	'''
		Method to set bundle differences and total
	'''
	#Calculating based on Items table
	total_bundle_amount = 0
	for row in doc.items:
		if row.from_bundle_item:
			total_bundle_amount += row.amount
	doc.total_bundle_amount = round(total_bundle_amount, 2)

	#Calculating based on Bundle Items Table
	total_bundle_amount_actual = 0
	for item in doc.bundle_items:
		if item.basic_rate and item.qty:
			item.amount = item.basic_rate * item.qty
		total_bundle_amount_actual += item.amount
	doc.total_bundle_amount_actual = round(total_bundle_amount_actual, 2)

	# Setting Difference Amount
	doc.bundle_difference_amount = round((doc.total_bundle_amount_actual - doc.total_bundle_amount), 2)

def validate_bundle_amount_difference(doc):
	"""
	Method to validate bundle difference amount on submission of stock entry
	"""
	if doc.ignore_bundle_difference:
		return
	if doc.bundle_difference_amount >=1 or doc.bundle_difference_amount <= -1:
		title = 'Check Difference Amount'
		msg = 'Cannot submit the stock entry due to difference amount of {0} for bundle items'.format(frappe.bold(doc.bundle_difference_amount))
		frappe.throw(title=title, msg=msg)

@frappe.whitelist()
def get_bundle_items(bundle_item):
	"""
	method fetches the child items of a bundle item to be added in the stock entry items table
	"""
	if not bundle_item:
		return []

	if not frappe.db.exists("Product Bundle", bundle_item):
		frappe.log_error(_("Product Bundle {0} does not exist").format(bundle_item))
		return []

	bundle_items = frappe.get_all(
		"Product Bundle Item",
		filters={"parent": bundle_item},
		fields=["item_code", "qty", "description"],
		order_by="idx"
	)
	result = []
	for row in bundle_items:
		item_details = frappe.get_cached_value(
			"Item",
			row.item_code,
			["item_name", "stock_uom", "standard_rate"],
			as_dict=True
		)
		result.append({
			"item_code": row.item_code,
			"item_name": item_details.item_name if item_details else "",
			"qty": flt(row.qty),
			"uom": item_details.stock_uom,
			"rate": item_details.standard_rate if item_details else 0,
			"description": row.description,
			"conversion_factor": 1,
			"from_bundle_item": 1
		})
	return result

@frappe.whitelist()
def process_bundle_items(bundle_items, from_warehouse=None, to_warehouse=None):
	"""
		Method processes the bundle items in the stock entry and updates the amounts
	"""
	data = []
	bundle_items = json.loads(bundle_items or "[]")

	for item in bundle_items:
		item_code = item.get("item_code")
		parent_qty = flt(item.get("qty"))
		parent_rate = flt(item.get("basic_rate"))

		if not item_code or parent_qty <= 0:
			continue

		bundle_data = get_bundle_items(item_code)

		for bundle_item in bundle_data:
			component_qty = flt(bundle_item.get("qty")) or 1

			total_qty = parent_qty * component_qty
			unit_rate = parent_rate / component_qty if component_qty else 0

			bundle_item.update({
				"qty": total_qty,
				"transfer_qty": total_qty,
				"rate": unit_rate,
				"basic_rate": unit_rate,
				"s_warehouse": item.get("s_warehouse") or from_warehouse,
				"t_warehouse": item.get("t_warehouse") or to_warehouse,
				"bundle_parent": item.get("name"),
				"expense_account": item.get("expense_account"),
				"from_bundle_item": 1
			})

		data.extend(bundle_data)

	return data

def unset_stn_exception(doc):
	'''
		Method to unset STN exception after Stock Entry submission
	'''
	frappe.db.sql("""
		UPDATE `tabAmazon STN Entry Item`
		SET error_log = ''
		WHERE stock_entry = %s
	""", (doc.name,))
