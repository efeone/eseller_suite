# Copyright (c) 2026, efeone and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate, today
from frappe.model.document import Document
from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_repository import get_orders

class AFNOrderShipmentLog(Document):
	def before_save(self):
		self.set_missing_values()

	def add_exceptions(self, exception_msg):
		'''
			Method to add exceptions if any
		'''
		if self.exceptions:
			exception_msg = f"{self.exceptions}\n{exception_msg}"
		self.exceptions = exception_msg

	def set_missing_values(self):
		'''
			Set missing values like Item, Warehosue and Company
		'''
		self.exceptions = ''
		#Setting Item
		if self.merchant_sku and not self.item_code:
			if frappe.db.exists('Item', self.merchant_sku):
				item_code = self.merchant_sku
				if frappe.db.get_value('Item', self.merchant_sku, 'actual_item'):
					item_code = frappe.db.get_value('Item', self.merchant_sku, 'actual_item')
				self.item_code = item_code
			else:
				exception_msg = f"Item not found with SKU : {self.merchant_sku}"
				self.add_exceptions(exception_msg)

		#Setting Item and Company
		if self.fc_code and not self.warehouse:
			if frappe.db.exists('Warehouse', { 'amazon_warehouse_code': self.fc_code }):
				self.warehouse = frappe.db.get_value('Warehouse', { 'amazon_warehouse_code': self.fc_code }, 'name')
				self.company = frappe.db.get_value('Warehouse', { 'amazon_warehouse_code': self.fc_code }, 'company')
			else:
				exception_msg = f"Warehouse not found FC : {self.fc_code}"
				self.add_exceptions(exception_msg)

		#Setting Order Created
		if frappe.db.exists('Sales Invoice', { 'amazon_order_id': self.amazon_order_id }):
			self.order_created = 1

		if self.company and self.warehouse and self.item_code:
			self.exceptions = ''
		self.has_exceptions = 1 if self.exceptions else 0

@frappe.whitelist()
def retry_fetching_selected_logs(docnames):
	'''
		Method to fetch missing values form list view
	'''
	if isinstance(docnames, str):
		docnames = frappe.parse_json(docnames)

	for name in docnames:
		if frappe.db.exists("AFN Order Shipment Log", name):
			doc = frappe.get_doc("AFN Order Shipment Log", name)
			if doc.has_exceptions:
				doc.set_missing_values()
				doc.save()
	return "Success"

@frappe.whitelist()
def check_rq_job_existance(amazon_order_id, amz_setting_name):
	'''
		Method to check wether RQ Job for Get Order is working or not
	'''
	scheduler_rq_jobs = frappe.db.get_all('RQ Job', {
		'job_name': 'eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings.schedule_get_order_details',
		'status': ['in', ['queued', 'started']]
	})
	sync_rq_jobs = frappe.db.get_all('RQ Job', {
		'job_name': f'Get Amazon Orders - {amz_setting_name}',
		'status': ['in', ['queued', 'started']]
	})
	if scheduler_rq_jobs or sync_rq_jobs:
		return 0
	return 1

@frappe.whitelist()
def fetch_sales_orders(docnames):
	'''
		Method to fetch missing values from list view
	'''
	last_updated_after = getdate(today())
	amz_setting_name = frappe.db.get_all('Amazon SP API Settings', { 'is_active':1 }, pluck='name', limit=1) or []
	if not amz_setting_name:
		frappe.msgprint("No active Amazon SP API Settings found. Please create and activate one to fetch orders.", indicator='red')
		return
	amz_setting_name = amz_setting_name[0]

	if isinstance(docnames, str):
		docnames = frappe.parse_json(docnames)

	filtered_order_ids = frappe.db.get_all(
		"AFN Order Shipment Log",
		filters={"name": ["in", docnames], "has_exceptions": 0},
		pluck="amazon_order_id",
		limit=20
	)

	# Remove duplicates
	unique_order_ids = list(set(filtered_order_ids))

	# Convert to comma separated string
	amazon_order_ids = ",".join(unique_order_ids)

	orders = get_orders(amz_setting_name=amz_setting_name, last_updated_after=last_updated_after, amazon_order_ids=amazon_order_ids)
	return orders

@frappe.whitelist()
def get_sales_order_item_codes(amazon_order_id):
	sales_order = frappe.db.get_value("Sales Order", {"amazon_order_id": amazon_order_id}, "name")

	if not sales_order:
		return []

	return frappe.get_all("Sales Order Item", filters={"parent": sales_order}, pluck="item_code")

@frappe.whitelist()
def get_sales_order_items(doctype, txt, searchfield, start, page_len, filters):
	"""Returns item_codes that exist as line items in the given Sales Order."""
	sales_order = filters.get('sales_order')
	if not sales_order:
		return []

	return frappe.db.sql("""
		SELECT soi.item_code, soi.item_name
		FROM `tabSales Order Item` soi
		WHERE soi.parent = %(sales_order)s
		AND soi.item_code LIKE %(txt)s
		ORDER BY soi.idx
		LIMIT %(start)s, %(page_len)s
	""", {
		'sales_order': sales_order,
		'txt': f'%{txt}%',
		'start': start,
		'page_len': page_len
	})


@frappe.whitelist()
def update_sales_order_item(sales_order, old_item_code, new_item_code):
	"""Replace old_item_code with new_item_code on the given Sales Order's item row."""
	if not (sales_order and old_item_code and new_item_code):
		frappe.throw(_('Sales Order, Sales Order Item, and New Item are all required.'))

	so = frappe.get_doc('Sales Order', sales_order)

	matched_row = None
	for item in so.items:
		if item.item_code == old_item_code:
			matched_row = item
			break

	if not matched_row:
		frappe.throw(_('Item {0} not found in Sales Order {1}').format(old_item_code, sales_order))

	matched_row.item_code = new_item_code
	reset_fc_processed_flag(so.amazon_order_id, new_item_code)
	so.flags.ignore_permissions = True  # Ignore permissions to allow updates
	so.submit()
	return {'success': True}

def reset_fc_processed_flag(amazon_order_id, item_code):
	'''
		Method to mark the shipment logs of an item as not processed

		Logs are flagged as processed even when their item_code matched no row on the
		Sales Order. Clearing the flag lets the Sales Order pick up the FC details of
		the item on submit, which is mandatory for an AFN order.
	'''
	if not amazon_order_id:
		return

	filters = {
		'amazon_order_id': amazon_order_id,
		'item_code': item_code,
		'has_exceptions': 0
	}
	if frappe.db.exists('AFN Order Shipment Log', filters):
		frappe.db.set_value('AFN Order Shipment Log', filters, 'fc_processed', 0)
