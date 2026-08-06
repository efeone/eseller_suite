import frappe
from frappe import _
from frappe.utils import cint, flt
from frappe.model.mapper import get_mapped_doc
from frappe.model.utils import get_fetch_values
from frappe.desk.doctype.tag.tag import add_tag
from frappe.contacts.doctype.address.address import get_company_address

from erpnext.accounts.party import get_party_account
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults
from erpnext.stock.doctype.item.item import get_item_defaults

from eseller_suite.eseller_suite.utils import is_old_data

import re

class SalesOrderOverride(SalesOrder):
	def custom_validate(self):
		total_qty = 0
		total = 0
		total_taxes_and_charges = 0
		for item in self.items:
			qty = int(item.qty)
			rate = float(item.rate)
			amount = rate*qty
			item.base_rate = rate
			item.amount = amount
			item.base_amount = amount
			item.uom = item.stock_uom
			if self.delivery_date:
				item.delivery_date = self.delivery_date
			total_qty += qty
			total += amount

		for tax_row in self.taxes:
			if tax_row.tax_amount:
				total_taxes_and_charges += float(tax_row.tax_amount)

		self.total = total
		self.total_qty = total_qty
		self.total_taxes_and_charges = total_taxes_and_charges
		self.grand_total = total + total_taxes_and_charges
		self.base_grand_total = total + total_taxes_and_charges

	def before_validate(self):
		self.set_internal_amazon_order_id()
		super(SalesOrderOverride, self).before_validate()

	def validate(self):
		self.set_internal_amazon_order_id()
		self.custom_validate()
		recall_order_prefixes = ['S']
		super(SalesOrderOverride, self).validate()
		self.process_fc_based_changes()
		if self.amazon_order_status != 'Canceled' and not self.amazon_order_amount and self.amazon_order_id and self.amazon_order_id[0] not in recall_order_prefixes:
			self.amazon_order_amount = self.total
		if self.amazon_order_id and self.amazon_order_id[0] in recall_order_prefixes:
			self.amazon_order_amount =  0
		if self.amazon_order_status == 'Canceled' or self.replaced_order_id:
			self.amazon_order_amount =  0
		self.set_has_multi_company_exception_flag()

	def on_submit(self):
		if self.has_multi_company_exception:
			frappe.throw("Multi Company Exception is not handled")
		super(SalesOrderOverride, self).on_submit()

		# ignore Invoice Creation for old orders
		if not is_old_data(self.transaction_date):
			sales_invoice = make_sales_invoice(source_name=self.name, target_doc=None, ignore_permissions=True)
			sales_invoice.update_stock = 1
			sales_invoice.insert(ignore_permissions=True)

			self.update_shipment_logs(submit=1)

	def on_update(self):
		self.update_shipment_logs()
		if self.amazon_order_status == "Canceled" and self.temporary_stock_tranfer_id:
			if frappe.db.exists("Stock Entry", {"name":self.temporary_stock_tranfer_id, "docstatus":["!=", 2]}):
				temp_stock_transfer_doc = frappe.get_doc("Stock Entry", self.temporary_stock_tranfer_id)
				self.temporary_stock_tranfer_id = ""
				self.save(ignore_permissions=True)
				if temp_stock_transfer_doc.docstatus == 1:
					temp_stock_transfer_doc.cancel()
				temp_stock_transfer_doc.delete()

	def after_insert(self):
		amz_setting = frappe.get_last_doc("Amazon SP API Settings", {"is_active":1})
		if amz_setting.temporary_stock_transfer_required and self.amazon_order_id and self.amazon_order_status != "Canceled":
			self.create_temporary_stock_transfer()

	def after_delete(self):
		"""method deletes the temporary stock entry if it exists"""
		if self.temporary_stock_tranfer_id:
			if frappe.db.exists("Stock Entry", {"name":self.temporary_stock_tranfer_id, "docstatus":["!=", 2]}):
				temp_stock_transfer_doc = frappe.get_doc("Stock Entry", self.temporary_stock_tranfer_id)
				if temp_stock_transfer_doc.docstatus == 1:
					temp_stock_transfer_doc.cancel()
				temp_stock_transfer_doc.delete()
		if self.amazon_order_id:
			frappe.db.set_value('AFN Order Shipment Log', {'amazon_order_id': self.amazon_order_id}, 'order_created', 0)
			frappe.db.set_value('AFN Order Shipment Log', {'amazon_order_id': self.amazon_order_id}, 'invoice_created', 0)
			frappe.db.set_value('AFN Order Shipment Log', {'amazon_order_id': self.amazon_order_id}, 'fc_processed', 0)

	def before_submit(self):
		if self.amazon_order_id:
			self.validate_amazon_so_submit()

	def create_temporary_stock_transfer(self):
		"""method creates a stock entry to the temporary warehoue when a sales order is screated
		"""
		temp_stock_entry = frappe.new_doc("Stock Entry")
		temp_stock_entry.stock_entry_type = "Material Transfer"
		temp_stock_entry.set_posting_time = 1
		temp_stock_entry.posting_date = self.transaction_date
		temp_stock_entry.posting_time = self.transaction_time
		amz_setting = frappe.db.exists("Amazon SP API Settings", {"is_active":1})
		warehouse = frappe.db.get_value("Amazon SP API Settings", amz_setting, "warehouse")
		if self.fulfillment_channel:
			if self.fulfillment_channel=='AFN':
				warehouse = frappe.db.get_value("Amazon SP API Settings", amz_setting, "afn_warehouse")
		for item in self.items:
			temp_stock_entry.append("items", {
				"s_warehouse": warehouse,
				"t_warehouse": frappe.db.get_value("Amazon SP API Settings", amz_setting, "temporary_order_warehouse"),
				"item_code": item.item_code,
				"qty": item.qty,
				"allow_zero_valuation_rate": 1,
			})
		temp_stock_entry.insert(ignore_permissions=True)
		self.temporary_stock_tranfer_id = temp_stock_entry.name
		self.save(ignore_permissions=True)
		frappe.db.savepoint("before_temp_stock_entry_submit")
		try:
			temp_stock_entry.submit()
		except Exception as e:
			frappe.db.rollback(save_point="before_temp_stock_entry_submit")
			failed_sync_record = frappe.new_doc('Amazon Failed Sync Record')
			failed_sync_record.amazon_order_id = self.amazon_order_id
			failed_sync_record.remarks = "Failed to create temporary stock entry\n" + str(e)
			failed_sync_record.save(ignore_permissions=True)

	def validate_amazon_so_submit(self):
		"""method validates the amazon sales order"""
		order_statuses = [
			"Shipped",
			"InvoiceUnconfirmed",
			"Unfulfillable",
		]

		order_status_valid = self.amazon_order_status in order_statuses

		# Check if taxes are set for the order
		# If the order is a replacement order, taxes are already handled via jv
		has_taxes = len(self.taxes) > 0 if not self.replaced_order_id else True

		transfer_flag = True
		if self.temporary_stock_tranfer_id:
			transfer_flag = frappe.db.exists("Stock Entry", {
				"name": self.temporary_stock_tranfer_id,
				"docstatus": 1
			})
		fc_data_exists = True
		if self.fulfillment_channel == "AFN" and not self.ignore_fc_changes:
			fc_data_exists = True if self.fc_location else False

		if not (order_status_valid and has_taxes and transfer_flag and fc_data_exists):
			msg = f"Sales Order {self.name} cannot be submitted."
			if not order_status_valid:
				msg += " Ensure that the order status is valid."
			if order_status_valid and not has_taxes and not self.replaced_order_id:
				msg += " Ensure that taxes are set for the order."
			if order_status_valid and not transfer_flag:
				msg += " Ensure that the temporary stock transfer has been completed."
			if not fc_data_exists:
				msg += " Ensure FC Details has been fetched."
			frappe.throw(
				_(msg),
				title=_("Invalid Sales Order Submission"),
				exc=frappe.ValidationError
			)

	def process_fc_based_changes(self):
		"""
			Handle FC-based warehouse & company changes.
			Supports multiple logs per item_code (qty split across warehouses).
		"""
		if not self.amazon_order_id or self.fulfillment_channel != "AFN" or self.ignore_fc_changes:
			return

		companies = frappe.db.get_all(
			'AFN Order Shipment Log',
			filters={'amazon_order_id': self.amazon_order_id},
			pluck='company',
			distinct=True
		)

		# Getting Tag for Multi Company Exception
		fc_exception_tag = None
		amz_setting = frappe.db.exists("Amazon SP API Settings", {"is_active":1})
		if amz_setting:
			fc_exception_tag = frappe.db.get_value("Amazon SP API Settings", amz_setting, "fc_exception_tag")

		if len(companies)>1:
			if fc_exception_tag:
				#Check for document existance as it's called in validate
				if frappe.db.exists(self.doctype, self.name):
					add_tag(fc_exception_tag, self.doctype, self.name)
			remarks = 'Can not update Sales Order, Multiple companies found in shipment logs. FC-based changes cannot be processed. Please check the shipment logs for this order.'
			if not frappe.db.exists('Amazon Failed Sync Record', {'amazon_order_id': self.amazon_order_id, 'remarks': remarks}):
				failed_sync_record = frappe.new_doc("Amazon Failed Sync Record")
				failed_sync_record.amazon_order_id = self.amazon_order_id
				failed_sync_record.remarks = remarks
				failed_sync_record.is_multi_company_exception = 1
				failed_sync_record.save(ignore_permissions=True)
			return

		shipment_logs = frappe.db.get_all(
			'AFN Order Shipment Log',
			filters={'amazon_order_id': self.amazon_order_id, 'fc_processed':0, 'has_exceptions':0 },
			fields=['fc_code', 'item_code', 'warehouse', 'company', 'qty']
		)

		if not shipment_logs:
			return

		# Group logs by item_code
		from collections import defaultdict
		# Structure:
		# {
		# 	'CS8502 X 2PK': [
		# 		{'warehouse': 'DEL4', 'qty': 20.0, 'company': 'Company A', 'fc_code': 'FC1'},
		# 		{'warehouse': 'BLR2', 'qty': 10.0, 'company': 'Company A', 'fc_code': 'FC2'},
		# 	]
		# }
		# Group by item_code -> (warehouse, company, fc_code)
		temp_map = defaultdict(lambda: defaultdict(float))

		for log in shipment_logs:
			key = (log.warehouse, log.company, log.fc_code)
			temp_map[log.item_code][key] += log.qty

		# Convert to required structure
		final_map = {}

		for item_code, group_data in temp_map.items():
			final_map[item_code] = []

			for (warehouse, company, fc_code), qty in group_data.items():
				final_map[item_code].append({
					"warehouse": warehouse,
					"company": company,
					"fc_code": fc_code,
					"qty": qty
				})

		has_company_change = False
		new_items = []

		for row in self.items:
			if row.fc_location:
				new_items.append(row)
				continue

			logs = final_map.get(row.item_code)

			# If no matching logs → keep row as is
			if not logs:
				new_items.append(row)
				continue

			remaining_qty = row.qty

			for log in logs:
				if remaining_qty <= 0:
					break

				split_qty = min(remaining_qty, log.get("qty", 0))
				remaining_qty -= split_qty

				# Duplicate row with split qty and log values
				new_row = row.as_dict()
				new_row.pop("name", '')
				new_row.pop("idx", '')
				new_row["qty"] = split_qty
				new_row["warehouse"] = log.get("warehouse", '')
				new_row["fc_location"] = log.get("fc_code", '')
				new_items.append(new_row)

				if self.company != log.get('company', ''):
					has_company_change = True

				# Update header values based on last match
				self.company = log.get('company', self.company)
				self.set_warehouse = log.get("warehouse", self.set_warehouse)
				self.fc_location = log.get("fc_code", '')

			# Duplicate row with split qty and log values
			if remaining_qty > 0:
				new_row_1 = row.as_dict()
				new_row_1.pop("name", '')
				new_row_1.pop("idx", '')
				new_row_1["qty"] = remaining_qty
				new_items.append(new_row_1)

		frappe.db.set_value('AFN Order Shipment Log', {'amazon_order_id': self.amazon_order_id, 'fc_processed':0, 'has_exceptions':0}, 'fc_processed', 1)

		# Clear and re-add items
		self.set("items", [])
		for d in new_items:
			self.append("items", d)

		if has_company_change:
			self.handle_company_changes()

	def handle_company_changes(self):
		'''
			Method to change all fields relevent to company
		'''
		if self.fulfillment_channel != "AFN":
			return

		self.company_address = get_company_address(self.company).get('company_address') or ''
		company_abr, default_cc = frappe.db.get_value('Company', self.company, ['abbr', 'cost_center'])
		for row in self.items:
			if row.cost_center:
				current_cc = row.cost_center
				new_cc = re.sub(r"-[^-]*$", f"- {company_abr}", current_cc)
				row.cost_center = new_cc if frappe.db.exists('Cost Center', new_cc) else default_cc
			if row.item_tax_template:
				current_tax_temp = row.item_tax_template
				new_tax_temp = re.sub(r"-[^-]*$", f"- {company_abr}", current_tax_temp)
				row.item_tax_template = new_tax_temp if frappe.db.exists('Item Tax Template', new_tax_temp) else ''
		self.packed_items = []

		for row in self.taxes:
			if row.cost_center:
				current_cc = row.cost_center
				new_cc = re.sub(r"-[^-]*$", f"- {company_abr}", current_cc)
				row.cost_center = new_cc if frappe.db.exists('Cost Center', new_cc) else default_cc
			if row.account_head:
				row.account_head = get_account_head(row.account_head, self.company)

	def update_shipment_logs(self, submit=0):
		'''
			Method to update shipment logs with invoiced as 1 when sales order is submitted
		'''
		if self.amazon_order_id:
			if frappe.db.exists('AFN Order Shipment Log', {'amazon_order_id': self.amazon_order_id}):
				if submit:
					frappe.db.set_value('AFN Order Shipment Log', {'amazon_order_id': self.amazon_order_id}, 'invoice_created', 1, update_modified=False)
				else:
					frappe.db.set_value('AFN Order Shipment Log', {'amazon_order_id': self.amazon_order_id}, 'order_created', 1, update_modified=False)

	def set_has_multi_company_exception_flag(self):
		'''
			Method to set multi company flag in case of multiple companies in shipment logs
		'''
		amz_setting = frappe.db.exists("Amazon SP API Settings", {"is_active":1})
		if amz_setting:
			fc_exception_tag = frappe.db.get_value("Amazon SP API Settings", amz_setting, "fc_exception_tag")
			if fc_exception_tag:
				if frappe.db.exists('Tag Link', { 'document_type':self.doctype, 'document_name': self.name, 'tag': fc_exception_tag}):
					self.has_multi_company_exception = 1

	def set_internal_amazon_order_id(self):
		'''
			Method to set internal amazon order id for the sales order based on Company
		'''
		if self.amazon_order_id and self.company:
			company_abr = frappe.db.get_value('Company', self.company, 'abbr')
			self.amazon_order_id_internal = f"{self.amazon_order_id}-{company_abr}"
		if self.is_split_order:
			self.amazon_order_id_internal = f"{self.amazon_order_id}-{company_abr}-S"
		if self.docstatus == 0:
			if frappe.db.exists('Sales Order', {'amazon_order_id_internal': self.amazon_order_id_internal, 'name': ['!=', self.name]}):
				frappe.db.delete('Sales Order', self.name)

def get_account_head(current_acc, company):
	'''
		Get account head based on company
	'''
	company_abr = frappe.db.get_value('Company', company, 'abbr')
	new_account = re.sub(r"-[^-]*$", f"- {company_abr}", current_acc)
	if frappe.db.exists('Account', new_account):
		return new_account
	current_acc_parent = frappe.db.get_value('Account', current_acc, 'parent_account')
	new_account_doc = frappe.new_doc("Account")
	new_account_doc.account_name = current_acc.rsplit("-", 1)[0].strip()
	new_account_doc.company = company
	new_account_doc.parent_account = get_account_head(current_acc_parent, company)
	new_account_doc.insert(ignore_permissions=True)
	return new_account_doc.name

@frappe.whitelist()
def make_sales_invoice(source_name, target_doc=None, ignore_permissions=False):
	def postprocess(source, target):
		set_missing_values(source, target)
		# Get the advance paid Journal Entries in Sales Invoice Advance
		if target.get("allocate_advances_automatically"):
			target.set_advances()

	def set_missing_values(source, target):
		target.flags.ignore_permissions = True
		target.run_method("set_missing_values")
		target.run_method("set_po_nos")
		target.run_method("calculate_taxes_and_totals")
		target.run_method("set_use_serial_batch_fields")

		if source.company_address:
			target.update({"company_address": source.company_address})
		else:
			# set company address
			target.update(get_company_address(target.company))

		if target.company_address:
			target.update(get_fetch_values("Sales Invoice", "company_address", target.company_address))

		# set the redeem loyalty points if provided via shopping cart
		if source.loyalty_points and source.order_type == "Shopping Cart":
			target.redeem_loyalty_points = 1

		target.debit_to = get_party_account("Customer", source.customer, source.company)
		target.set_posting_time = 1

	def update_item(source, target, source_parent):
		target.allow_zero_valuation_rate = 1
		target.amount = flt(source.amount) - flt(source.billed_amt)
		target.base_amount = target.amount * flt(source_parent.conversion_rate)
		target.qty = (
			target.amount / flt(source.rate)
			if (source.rate and source.billed_amt)
			else source.qty - source.returned_qty
		)
		target.total_order_value = source.total_order_value

		if source_parent.project:
			target.cost_center = frappe.db.get_value("Project", source_parent.project, "cost_center")
		if target.item_code:
			item = get_item_defaults(target.item_code, source_parent.company)
			item_group = get_item_group_defaults(target.item_code, source_parent.company)
			cost_center = item.get("selling_cost_center") or item_group.get("selling_cost_center")

			if cost_center:
				target.cost_center = cost_center

	doclist = get_mapped_doc(
		"Sales Order",
		source_name,
		{
			"Sales Order": {
				"doctype": "Sales Invoice",
				"field_map": {
					"party_account_currency": "party_account_currency",
					"payment_terms_template": "payment_terms_template",
					"transaction_date": "posting_date",
					"transaction_time": "posting_time",
					"delivery_date": "due_date",
					"amazon_order_id": "amazon_order_id",
					"amazon_order_status": "amazon_order_status",
					"amazon_customer_type": "amazon_customer_type",
					"fulfillment_channel": "fulfillment_channel",
					"replaced_order_id": "replaced_order_id",
					"amazon_order_amount": "amazon_order_amount",
				},
				"field_no_map": ["payment_terms_template"],
				"validation": {"docstatus": ["=", 1]},
			},
			"Sales Order Item": {
				"doctype": "Sales Invoice Item",
				"field_map": {
					"name": "so_detail",
					"parent": "sales_order",
					"amazon_order_item_id": "amazon_order_item_id"
				},
				"postprocess": update_item,
				"condition": lambda doc: doc.qty
				and (doc.base_amount == 0 or abs(doc.billed_amt) < abs(doc.amount)),
			},
			"Sales Taxes and Charges": {"doctype": "Sales Taxes and Charges", "add_if_empty": True},
			"Sales Team": {"doctype": "Sales Team", "add_if_empty": True},
		},
		target_doc,
		postprocess,
		ignore_permissions=ignore_permissions,
	)

	automatically_fetch_payment_terms = cint(
		frappe.db.get_single_value("Accounts Settings", "automatically_fetch_payment_terms")
	)
	if automatically_fetch_payment_terms:
		doclist.set_payment_schedule()

	return doclist

@frappe.whitelist()
def split_so_based_on_company(sales_order):
	'''
		Splits the sales order based on company in case of FC orders with multiple companies in shipment logs
	'''
	split_item_with_taxes(sales_order)
	if not frappe.db.exists("Sales Order", sales_order):
		return

	so_doc = frappe.get_doc("Sales Order", sales_order)

	companies = frappe.db.get_all(
		'AFN Order Shipment Log',
		filters={'amazon_order_id': so_doc.amazon_order_id},
		pluck='company',
		distinct=True
	)

	# Fetch ALL shipment logs across ALL companies once, so allocation decisions
	# (especially exact-qty matches) are made with full visibility instead of
	# being scoped to whichever company happens to be processed first.
	all_shipment_logs = frappe.db.get_all(
		'AFN Order Shipment Log',
		filters={'amazon_order_id': so_doc.amazon_order_id, 'has_exceptions': 0},
		fields=['fc_code', 'item_code', 'warehouse', 'company', 'qty']
	)

	# item_code -> list of {warehouse, fc_code, company, remaining_qty}
	# Keyed by (company, warehouse) so slots from different companies never merge.
	log_consumption = {}
	for log in all_shipment_logs:
		item_map = log_consumption.setdefault(log.item_code, {})
		key = (log.company, log.warehouse)
		if key in item_map:
			item_map[key]["remaining_qty"] += log.qty
		else:
			item_map[key] = {
				"warehouse": log.warehouse,
				"fc_code": log.fc_code,
				"company": log.company,
				"remaining_qty": log.qty
			}
	log_consumption = {
		item_code: list(slot_map.values())
		for item_code, slot_map in log_consumption.items()
	}

	# --- Allocation planning pass: runs ONCE across all companies ---
	allocations = {company: {"items": [], "discount": 0, "warehouse": None} for company in companies}

	for item in so_doc.items:
		item_code = item.item_code
		if item_code not in log_consumption:
			continue

		remaining_item_qty = item.qty
		slots = log_consumption[item_code]

		# Priority: if some slot (any company) has remaining_qty exactly equal
		# to this row's qty, allocate the whole row there before falling back
		# to sequential/proportional consumption. E.g. row qty 4 vs slots
		# [CompanyA: 3, CompanyB: 1] won't match (correctly falls through to
		# the normal split logic below); row qty 3 vs [CompanyA: 3, CompanyB: 1]
		# matches CompanyA cleanly instead of being fragmented.
		exact_slot = next(
			(s for s in slots if s["remaining_qty"] == remaining_item_qty and remaining_item_qty > 0),
			None
		)
		ordered_slots = (
			[exact_slot] + [s for s in slots if s is not exact_slot]
			if exact_slot else slots
		)

		for slot in ordered_slots:
			if slot["remaining_qty"] <= 0 or remaining_item_qty <= 0:
				continue

			fulfil_qty = min(slot["remaining_qty"], remaining_item_qty)

			new_item = item.as_dict()
			new_item.pop("name", "")
			new_item.pop("idx", "")
			new_item["qty"] = fulfil_qty
			new_item["warehouse"] = slot["warehouse"]
			new_item["fc_location"] = slot["fc_code"]

			company = slot["company"]
			allocations[company]["items"].append((item, new_item))
			allocations[company]["discount"] += new_item.get("amazon_promotion_discount", 0)
			allocations[company]["warehouse"] = slot["warehouse"]

			slot["remaining_qty"] -= fulfil_qty
			remaining_item_qty -= fulfil_qty

	# --- Build & insert one Sales Order per company from the plan ---
	so_list = []
	for company in companies:
		alloc = allocations[company]
		if not alloc["items"]:
			continue

		new_so_doc = frappe.new_doc("Sales Order")
		new_so_doc.update(so_doc.as_dict())
		new_so_doc.items = []
		new_so_doc.taxes = []

		for item, new_item in alloc["items"]:
			new_so_doc.append("items", new_item)
			for tax in so_doc.taxes:
				new_tax = tax.as_dict()
				if item.shipment_log_id == new_tax.shipment_log_id:
					new_tax.pop("name", "")
					new_tax.pop("idx", "")
					new_so_doc.append("taxes", new_tax)

		new_so_doc.company = company
		new_so_doc.set_warehouse = alloc["warehouse"] or so_doc.set_warehouse
		new_so_doc.amazon_order_amount = 0
		new_so_doc.discount_amount = alloc["discount"]
		new_so_doc.has_multi_company_exception = 0
		new_so_doc.ignore_fc_changes = 1
		new_so_doc.is_split_order = 1
		new_so_doc.handle_company_changes()
		new_so_doc.insert(ignore_permissions=True)
		new_so_doc.submit()
		so_list.append(new_so_doc.name)

	frappe.db.delete("Sales Order", sales_order)
	frappe.db.delete("Amazon Failed Sync Record", {"amazon_order_id": so_doc.amazon_order_id})
	frappe.msgprint(
		_("Sales Order has been split into : {0}".format(", ".join(so_list))),
		alert=1,
		indicator="green"
	)
	return so_list

@frappe.whitelist()
def split_item_with_taxes(sales_order):
	'''
		Splits the sales order based on item with taxes in case of FC orders with multiple companies in shipment logs
	'''
	if frappe.db.exists("Sales Order", sales_order):
		so_doc = frappe.get_doc("Sales Order", sales_order)
		from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_repository import get_order_shipment_charges
		charges_and_fees = get_order_shipment_charges('98gq2qirqf', so_doc.amazon_order_id)
		item_shipment_details = charges_and_fees.get("item_shipment_details", [])

		from collections import defaultdict
		shipment_map = defaultdict(list)
		for d in item_shipment_details:
			shipment_map[d["amazon_order_item_id"]].append(d)

		updated_items = []

		for item in so_doc.items:
			shipments = shipment_map.get(item.amazon_order_item_id)

			if not shipments:
				updated_items.append(item.as_dict())
				continue

			total_shipment_qty = sum(s["qty"] for s in shipments)
			if total_shipment_qty != item.qty:
				frappe.msgprint(
					f"Shipment qty ({total_shipment_qty}) != item qty ({item.qty}) "
					f"for order_item_id {item.amazon_order_item_id}",
					indicator="orange"
				)

			for shipment in shipments:
				row = item.as_dict()
				for f in ("name", "idx"):
					row.pop(f, None)
				row["qty"] = shipment["qty"]
				row["shipment_log_id"] = shipment["shipment_log_id"]
				row["amazon_promotion_discount"] = shipment["promotion_discount"]
				row["amount"] = row["qty"] * row.get("rate", 0)
				updated_items.append(row)

		so_doc.set("items", [])
		for row in updated_items:
			so_doc.append("items", row)

		so_doc.set("taxes", [])
		for charge in charges_and_fees.get("charges"):
			if charge:
				so_doc.append("taxes", charge)

		for fee in charges_and_fees.get("fees"):
			if fee:
				so_doc.append("taxes", fee)

		for tds in charges_and_fees.get("tds"):
			if tds:
				so_doc.append("taxes", tds)

		so_doc.save()
