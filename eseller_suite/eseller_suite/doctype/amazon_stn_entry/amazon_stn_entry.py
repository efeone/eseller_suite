# Copyright (c) 2026, efeone and contributors
# For license information, please see license.txt
import frappe
from frappe.utils import flt
from frappe.model.document import Document
from frappe.utils.xlsxutils import make_xlsx
from frappe.utils.background_jobs import get_queue, execute_job

import os
import re
import csv
from bs4 import BeautifulSoup
from datetime import datetime
from charset_normalizer import from_path

from eseller_suite.eseller_suite.utils import add_bundle_components_to_stock_entry, get_bundle_items


class AmazonSTNEntry(Document):
	def validate(self):
		if not self.stn_entries:
			self.process_stn_file()
		self.set_ready_to_process_doc()

	def on_submit(self):
		# Kickoff job waits for the submit to commit, then builds the RQ chain
		frappe.enqueue(
			method="eseller_suite.eseller_suite.doctype.amazon_stn_entry.amazon_stn_entry.queue_stn_batch_chain",
			queue="short",
			is_async=True,
			enqueue_after_commit=True,
			docname=self.name,
		)

	def before_cancel(self):
		self.cancel_linked_documents()

	def on_trash(self):
		self.delete_linked_documents()

	def process_stn_file(self):
		"""Fetch and validate the attached STN CSV file."""
		if not self.stn_file:
			return

		attached_file = frappe.get_doc("File", {"file_url": self.stn_file})
		file_path = frappe.get_site_path("private", "files", attached_file.file_name)

		if not os.path.exists(file_path):
			frappe.throw(f"STN File not found at path: {file_path}")

		if attached_file.file_url.lower().endswith(".csv"):
			self.process_stn_csv(file_path)
		else:
			frappe.throw("Unsupported file format. Only CSV files are supported for STN File.")

	def process_stn_csv(self, file_path):
		"""Read CSV and append rows to child table."""
		try:
			detected = from_path(file_path).best()
			encoding = detected.encoding

			with open(file_path, "r", encoding=encoding) as file:
				csv_reader = csv.DictReader(file)

				for row in csv_reader:
					self.save_stn_entry_row(row)

		except Exception as e:
			error_message = f"Error processing STN CSV file: {str(e)}"
			frappe.throw(error_message)

	def save_stn_entry_row(self, row):
		"""Map CSV row and append to STN Entries."""
		key_mapping = {
			"Gstin Of Receiver": "target_gstin",
			"Transaction Type": "transaction_type",
			"Transaction Id": "transaction_id",
			"Order Id": "order_id",
			"Ship From Fc": "source_fc",
			"Ship From City": "source_fc_city",
			"Ship From State": "source_fc_state",
			"Ship From Postal Code": "source_fc_pin",
			"Ship To Fc": "target_fc",
			"Ship To City": "target_fc_city",
			"Ship To State": "target_fc_state",
			"Ship To Postal Code": "target_fc_pin",
			"Invoice Number": "invoice_number",
			"Invoice Date": "invoice_date_str",
			"Invoice Value": "invoice_value",
			"Asin": "asin",
			"Quantity": "qty",
			"Hsn Code": "hsn_code",
			"Taxable Value": "taxable_value",
			"Igst Rate": "igst_rate",
			"Igst Amount": "igst_amount",
			"Gstin Of Supplier": "source_gstin",
			"Irn Number": "irn_number",
			"Irn Filing Status": "irn_filing_status",
			"Irn Date": "irn_date",
		}

		stn_row = {}
		is_blank_row = True

		for csv_key, fieldname in key_mapping.items():
			value = row.get(csv_key)
			if value:
				is_blank_row = False
				stn_row[fieldname] = value.strip() if isinstance(value, str) else value

		if is_blank_row:
			return

		date_str = stn_row.get("invoice_date_str")
		if date_str:
			try:
				# 2025-04-30 10:44:48
				dt = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S")
				stn_row["invoice_date"] = dt.strftime("%Y-%m-%d")
				stn_row["invoice_time"] = dt.strftime("%H:%M:%S")
			except:
				try:
					# 30/04/2025 10:44
					dt = datetime.strptime(date_str.strip(), "%d/%m/%Y %H:%M")
					stn_row["invoice_date"] = dt.strftime("%Y-%m-%d")
					stn_row["invoice_time"] = dt.strftime("%H:%M:%S")
				except ValueError as e:
					stn_row['error_log'] = f"Invalid Invoice Date Format : Failed on: '{date_str}' | Error: {e}"
					stn_row['ready_to_process'] = 0

		child = self.append("stn_entries", stn_row)
		self.map_companies_and_warehouses(child)
		self.map_and_update_item(child)
		self.set_ready_to_process(child)
		self.check_duplicate_stn_entry(child)

	def map_companies_and_warehouses(self, row):
		"""Map companies and warehouses for the STN row."""
		allowed_transaction_types = ['FC_TRANSFER', 'FC_REMOVAL', 'FC_REMOVAL-Cancel', 'EInvoiceCancel']
		if row.transaction_type not in allowed_transaction_types:
			self.add_error_log(row, f"Transaction Type with {row.transaction_type} is not handled right now.")
			row.ready_to_process = 0
			return

		# Set source company and warehouse
		row.source_company = self.get_company_from_gstin(row.source_gstin)
		if row.source_company:
			if not row.source_fc:
				row.source_warehouse = ''
				self.add_error_log(row, f"Source Warehouse code is empty")
				return
			source_wh = self.get_warehouse_by_code(code=row.source_fc, row=row)
			if source_wh:
				row.source_warehouse = source_wh
			else:
				self.add_error_log(row,f"Source Warehouse not found for code: {row.source_fc} and company: {row.source_company}")
		else:
			self.add_error_log(row, f"Source Company not found for GSTIN: {row.source_gstin}")

		if row.transaction_type in ['FC_REMOVAL', 'FC_REMOVAL-Cancel', 'EInvoiceCancel']:
			main_warehouse = frappe.db.get_single_value("eSeller Settings", "main_warehouse")
			main_company = frappe.db.get_single_value("eSeller Settings", "main_company")
			if not main_warehouse or not main_company:
				self.add_error_log(row, f"Main Warehouse or Main Company is not configured in eSeller Settings, It is required to process {row.transaction_type}.")
				return
			row.target_company = main_company
			row.target_warehouse = main_warehouse
			return

		# Set target company and warehouse
		row.target_company = self.get_company_from_gstin(row.target_gstin)
		if row.target_company:
			if not row.target_fc:
				row.target_fc = ''
				self.add_error_log(row, f"Target Warehouse code is empty")
				return
			target_wh = self.get_warehouse_by_code(code=row.target_fc, row=row)
			if target_wh:
				row.target_warehouse = target_wh
			else:
				self.add_error_log(row, f"Target Warehouse not found for code: {row.target_fc} and company: {row.target_company}")
		else:
			self.add_error_log(row, f"Target Company not found for GSTIN: {row.target_gstin}")

	def get_warehouse_by_code(self, code, row):
		"""Get or create warehouse by Amazon code for given company."""
		allow_create = frappe.get_single_value("eSeller Settings", "allow_missing_warehouse_creation")
		wh = frappe.db.get_value("Warehouse", {"amazon_warehouse_code": code}, "name")

		if wh:
			return wh
		if allow_create:
			city = row.source_fc_city if code == row.source_fc else row.target_fc_city
			state = row.source_fc_state if code == row.source_fc else row.target_fc_state
			pin = row.source_fc_pin if code == row.source_fc else row.target_fc_pin
			company = row.source_company if code == row.source_fc else row.target_company
			return self.create_missing_warehouse(row=row, code=code, city=city, state=state, pin=pin, company=company)
		return None

	def create_missing_warehouse(self, row, code, city, state, pin, company):
		"""Create and return a missing Warehouse for the given company."""
		existing_wh = frappe.db.exists("Warehouse", {"amazon_warehouse_code": code})
		if existing_wh:
			return existing_wh

		parent_wh = self.get_parent_warehouse_for_company(company)
		if not parent_wh:
			self.add_error_log(row, f"No Parent Warehouse for Company {company}")
			return None

		try:
			wh = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": code,
				"amazon_warehouse_code": code,
				"city": city,
				"state": state,
				"pin": pin,
				"company": company,
				"parent_warehouse": parent_wh,
				"is_group": 0
			}).insert(ignore_permissions=True)

			return wh.name

		except Exception as e:
			exception_message = f"Error creating warehouse for code {code} and company {company}: {str(e)}"
			self.add_error_log(row, exception_message)
			return None

	def get_parent_warehouse_for_company(self, company):
		"""Return configured parent warehouse for the given company."""
		return frappe.db.get_value("eSeller Parent Warehouse",{"parent": "eSeller Settings", "company": company},"default_parent_warehouse")

	def get_company_from_gstin(self, gstin):
		"""Return Company mapped to the given GSTIN."""
		return frappe.db.get_value("Company", {"gstin": gstin}, "name")

	def get_item_with_asin(self, asin):
		"""
			Return Item mapped to the given ASIN.
			If there is actual Item Mapped, then Actual Item will be returned
		"""
		item_code = frappe.db.get_value("Item", {"amazon_item_code": asin}, "name")
		if item_code:
			if frappe.db.get_value("Item", item_code, "actual_item"):
				item_code = frappe.db.get_value("Item", item_code, "actual_item")
		return item_code

	def map_and_update_item(self, row):
		"""Map Item to row using ASIN and update HSN code if missing."""
		if row.asin:
			item = self.get_item_with_asin(row.asin)
			if item:
				row.item = item

				#Update HSN code in Item master if missing
				item_hsn, allow_purchase = frappe.db.get_value("Item", item, ["gst_hsn_code", "is_purchase_item"])
				if not item_hsn and row.hsn_code:
					frappe.db.set_value("Item", item, "gst_hsn_code", row.hsn_code, update_modified=False)

				# If item is not marked as purchase item and it's an inter-company transfer, mark it as purchase item
				if row.source_company != row.target_company and not allow_purchase:
					frappe.db.set_value('Item', item, 'is_purchase_item', 1, update_modified=False)
			else:
				self.add_error_log(row, f"Item not found for ASIN: {row.asin}")

	def add_error_log(self, row, error_message):
		"""Append error message to row's error_log field."""
		if row:
			if row.error_log:
				row.error_log += f"\n{error_message}"
			else:
				row.error_log = error_message
		else:
			frappe.log_error(message=f"Error log entry attempted but row is None: {error_message}", title="Amazon STN Entry Error")

	def set_ready_to_process(self, row):
		"""Set ready flag if all required mappings are available."""
		if (row.source_company and row.target_company and row.source_warehouse and row.target_warehouse and row.item):
			row.ready_to_process = 1
		else:
			row.ready_to_process = 0

	def set_ready_to_process_doc(self):
		'''
			Method to set ready_to_process on doc level if all row are ready to process
		'''
		self.ready_to_process = all(row.ready_to_process for row in self.stn_entries)

	@frappe.whitelist()
	def get_error_message_html(self):
		"""
			Generate HTML for error messages from child table entries.
		"""
		# Collect rows first (faster than string concatenation in loop)
		error_rows = []
		error_count_str = ''

		for entry in self.stn_entries:
			if entry.error_log:
				formatted_log = entry.error_log.replace("\n", "<br>")
				error_rows.append(
					f"<strong>Row {entry.idx}:</strong><br>{formatted_log}"
				)

		if not error_rows:
			return ""

		error_content = "<br><br>".join(error_rows)
		error_count_str = str(len(error_rows))

		error_html = f"""
			<div style="max-height: 300px; overflow-y: auto;">
				<div style="
					position: sticky;
					top: 0;
					background: #fff;
					padding: 10px;
					z-index: 10;
				">
					<strong style="color: #f00;">
						{error_count_str} error rows were found in the uploaded STN file :
					</strong>
				</div>
				<div style="padding: 10px;">
					{error_content}
				</div>
			</div>
		"""
		return error_html

	def add_item_to_stock_entry(self, se, row):
		"""
			Common method to add stock item or bundle item to Stock Entry.
			Returns True if item added successfully, else False.
		"""
		item_details = frappe.db.get_value("Item", row.item, ["is_stock_item", "is_bundle_item", "stock_uom"], as_dict=True)

		if not item_details:
			return False

		is_stock_item = item_details.is_stock_item
		is_bundle_item = item_details.is_bundle_item
		stock_uom = item_details.stock_uom
		if not is_stock_item and not is_bundle_item:
			error_message = f"{row.item} is not a stock Item and not a bundle item."
			frappe.db.set_value(row.doctype,row.name,{"error_log": error_message,"stock_entry": None,"transactions_created": 0}, update_modified=False)
			return False

		stock_uom = item_details.stock_uom
		if not is_stock_item and not is_bundle_item:
			return False

		qty = flt(row.qty)
		invoice_value = flt(row.invoice_value)
		basic_rate = invoice_value / qty if qty else 0
		if is_stock_item:
			se.append("items", {
				"item_code": row.item,
				"qty": qty,
				"transfer_qty": qty,
				"uom": stock_uom,
				"basic_rate": basic_rate,
				"amount": invoice_value,
				"base_amount": invoice_value,
				"s_warehouse": row.source_warehouse,
				"t_warehouse": row.target_warehouse,
				"basic_rate": basic_rate,
				"conversion_factor": 1,
				"allow_zero_valuation_rate": 1,
				"set_basic_rate_manually": 1
			})

		elif is_bundle_item:
			se.append("bundle_items", {
				"item_code": row.item,
				"qty": qty,
				"transfer_qty": qty,
				"uom": stock_uom,
				"basic_rate": basic_rate,
				"amount": invoice_value,
				"base_amount": invoice_value,
				"s_warehouse": row.source_warehouse,
				"t_warehouse": row.target_warehouse,
				"conversion_factor": 1,
				"allow_zero_valuation_rate": 1,
				"set_basic_rate_manually": 1
			})
			add_bundle_components_to_stock_entry(se=se, bundle_item_code=row.item, bundle_qty=qty, bundle_rate=basic_rate ,source_warehouse=row.source_warehouse, target_warehouse=row.target_warehouse)
		return True

	def create_stock_entries_for_material_transfer(self, row, stock_entry_type):
		"""
			Create Stock Entries for rows where Source Company equals Target Company.
		"""
		if row.source_company != row.target_company:
			return
		existing_stock_entry = frappe.db.get_value("Stock Entry", {"amazon_invoice_id": row.invoice_number, "docstatus": ["!=", 2]}, "name")
		if existing_stock_entry:
			se = frappe.get_doc("Stock Entry", existing_stock_entry)
			if se.docstatus == 1:
				frappe.db.set_value(row.doctype, row.name, {"stock_entry": existing_stock_entry, "transactions_created": 1}, update_modified=False)
				return

			if not self.add_item_to_stock_entry(se, row):
				self.add_error_log(row, f"{row.item} is not a stock or bundle item.")
				return
			se.amazon_invoice_value = flt(se.amazon_invoice_value) + flt(row.invoice_value)
			se.save(ignore_permissions=True)
			frappe.db.set_value(row.doctype, row.name, {"stock_entry": existing_stock_entry, "transactions_created": 1}, update_modified=False)
			return

		# Create Stock Entry
		se = frappe.new_doc('Stock Entry')
		se.stock_entry_type = stock_entry_type
		se.company = row.source_company
		se.posting_date = row.invoice_date
		se.posting_time = row.invoice_time
		se.set_posting_time = 1
		se.amazon_invoice_id = row.invoice_number
		se.from_warehouse = row.source_warehouse
		se.to_warehouse = row.target_warehouse

		if not self.add_item_to_stock_entry(se, row):
			self.add_error_log(row, f"{row.item} is not a stock or bundle item.")
			return
		se.amazon_invoice_value = flt(se.amazon_invoice_value) + flt(row.invoice_value)
		se.insert(ignore_permissions=True)
		frappe.db.set_value(row.doctype, row.name, {
			"stock_entry": se.name,
			"transactions_created": 1,
			"error_log": ""
		}, update_modified=False)

	def delete_linked_documents(self):
		"""Delete linked Stock Entries when the STN Entry is deleted."""
		# Delete linked submission queue entry
		if frappe.db.exists('Submission Queue', {"ref_doctype": self.doctype, "ref_docname": self.name}):
			frappe.db.delete('Submission Queue', {"ref_doctype": self.doctype, "ref_docname": self.name})

		# Delete linked Entries
		for row in self.stn_entries:
			if row.stock_entry:
				try:
					frappe.delete_doc("Stock Entry", row.stock_entry, ignore_permissions=True, force=True)
				except Exception as e:
					frappe.log_error(message=f"Failed to delete Stock Entry {row.stock_entry} linked to STN Entry {self.name}: {str(e)}", title="Amazon STN Entry Deletion Error")

			if row.sales_invoice:
				try:
					frappe.delete_doc("Sales Invoice", row.sales_invoice, ignore_permissions=True, force=True)
				except Exception as e:
					frappe.log_error(message=f"Failed to delete Sales Invoice {row.sales_invoice} linked to STN Entry {self.name}: {str(e)}", title="Amazon STN Entry Deletion Error")

			if row.purchase_invoice:
				try:
					frappe.delete_doc("Purchase Invoice", row.purchase_invoice, ignore_permissions=True, force=True)
				except Exception as e:
					frappe.log_error(message=f"Failed to delete Purchase Invoice {row.purchase_invoice} linked to STN Entry {self.name}: {str(e)}", title="Amazon STN Entry Deletion Error")

	def cancel_linked_documents(self):
		"""Cancel linked Stock Entries when the STN Entry is Cancelled."""
		# Cancel linked Entries
		for row in self.stn_entries:
			# Cancel linked Stock Entry
			if row.stock_entry:
				try:
					se = frappe.get_doc("Stock Entry", row.stock_entry)
					if se.docstatus == 1:
						se.cancel()
				except Exception as e:
					frappe.log_error(message=f"Failed to Cancel Stock Entry {row.stock_entry} linked to STN Entry {self.name}: {str(e)}", title="Amazon STN Entry Cancel Error")

			# Cancel linked Sales Invoice
			if row.sales_invoice:
				try:
					si = frappe.get_doc("Sales Invoice", row.sales_invoice)
					if si.docstatus == 1:
						si.cancel()
				except Exception as e:
					frappe.log_error(message=f"Failed to Cancel Sales Invoice {row.sales_invoice} linked to STN Entry {self.name}: {str(e)}", title="Amazon STN Entry Cancel Error")

			# Cancel linked Purchase Invoice
			if row.purchase_invoice:
				try:
					pi = frappe.get_doc("Purchase Invoice", row.purchase_invoice)
					if pi.docstatus == 1:
						pi.cancel()
				except Exception as e:
					frappe.log_error(message=f"Failed to Cancel Purchase Invoice {row.sales_invoice} linked to STN Entry {self.name}: {str(e)}", title="Amazon STN Entry Cancel Error")

	def handle_fc_removal_cancel(self, row):
		"""
			Cancel related documents for FC_REMOVAL - cancel transaction type.
		"""
		if not row.invoice_number:
			self.add_error_log(row, "Invoice number missing for FC_REMOVAL - cancel.")
			return

		found_any = False
		# Search for Stock Entry, Sales Invoice, Purchase Invoice with amazon_invoice_id = row.invoice_number
		for doctype in ["Stock Entry", "Sales Invoice", "Purchase Invoice"]:
			docs = frappe.get_all(doctype, filters={
				"amazon_invoice_id": row.invoice_number
			}, fields=["name", "docstatus"])

			if docs:
				found_any = True
				for d in docs:
					if d.docstatus == 2:
						continue

					if d.docstatus == 0:
						self.add_error_log(row, f"{doctype} {d.name} is in draft state.")

					try:
						doc = frappe.get_doc(doctype, d.name)
						doc.cancel()
					except Exception as e:
						self.add_error_log(row, f"Failed to cancel {doctype} {d.name}: {str(e)}")

		if not found_any:
			self.add_error_log(row, f"No related documents found for Invoice Number: {row.invoice_number}")
		else:
			frappe.db.set_value(row.doctype, row.name, "transactions_created", 1, update_modified=False)
			row.transactions_created = 1

	def handle_e_invoice_cancel(self, row):
		'''
			Method to handle e-invoice cancel
			Creating Credit/Debit notes against Inter compnay Sales/Purchase Invoice
		'''
		if not row.invoice_number:
			self.add_error_log(row, "Invoice number missing for EInvoiceCancel.")
			return
		log_doc = frappe.new_doc('STN ToDo Log')
		log_doc.stn_entry = self.name
		log_doc.stn_entry_item = row.name
		log_doc.invoice_number = row.invoice_number
		log_doc.invoice_value = row.invoice_value
		log_doc.save(ignore_permissions=True)
		self.add_error_log(row, f"EInvoiceCancel process for Invoice Number: {row.invoice_number} is queued.")

	def create_sales_invoice(self, row):
		"""
			Create Sales Invoice for Source Company
		"""
		if row.source_company == row.target_company:
			return

		existing_invoice = frappe.db.exists("Sales Invoice", {
			"amazon_invoice_id": row.invoice_number,
			"company": row.source_company,
			"docstatus": ["!=", 2]  # Exclude cancelled invoices
		})

		tax_rate = flt(row.igst_rate)*100
		igst_account = frappe.db.get_value("GST Account", {
			"company": row.source_company,
			"account_type": "Output",
			"parent": "GST Settings",
			"parentfield": "gst_accounts"
		}, "igst_account")
		item_tax_template = frappe.db.get_value("Item Tax Template",{
			"company": row.source_company,
			"gst_rate": tax_rate,
			"disabled":0
		}) or ""

		if existing_invoice:
			try:
				si = frappe.get_doc("Sales Invoice", existing_invoice)
				if si.docstatus == 1:
					frappe.db.set_value(row.doctype, row.name, "sales_invoice", existing_invoice, update_modified=False)
					return

				si.append("items", {
					"item_code": row.item,
					"qty": flt(row.qty),
					"rate": flt(row.taxable_value) / flt(row.qty) if flt(row.qty) else 0,
					"warehouse": row.source_warehouse,
					"allow_zero_valuation_rate": 1,
					"item_tax_template": item_tax_template
				})
				si.amazon_invoice_value = flt(si.amazon_invoice_value) + flt(row.invoice_value)
				si.save(ignore_permissions=True)
				frappe.db.set_value(row.doctype, row.name, "sales_invoice", existing_invoice, update_modified=False)
				return
			except Exception as e:
				exception_msg = f"Failed to create Sales Invoice: {str(e)}"
				if row.error_log:
					exception_msg = f"{row.error_log}\n{exception_msg}"
				frappe.db.set_value(row.doctype, row.name, "error_log", exception_msg, update_modified=False)
				self.add_error_log(row, exception_msg)
				return

		customer = frappe.db.get_value("Customer", {"represents_company": row.target_company, "is_internal_customer": 1})
		if not customer:
			self.add_error_log(row, f"Internal Customer for Company {row.target_company} not found.")
			return

		try:
			#Create Sales Invoice
			si = frappe.new_doc("Sales Invoice")
			si.customer = customer
			si.company = row.source_company
			si.posting_date = row.invoice_date
			si.posting_time = row.invoice_time
			si.set_posting_time = 1
			si.update_stock = 1
			si.amazon_invoice_id = row.invoice_number
			si.set_warehouse = row.source_warehouse
			si.disable_rounded_total = 1
			price_list = frappe.db.get_single_value("eSeller Settings", "inter_company_price_list")
			if price_list:
				si.selling_price_list = price_list
			stn_si_series = frappe.db.get_single_value("eSeller Settings", "stn_sales_invoice_series")
			if stn_si_series:
				si.naming_series = stn_si_series

			si.append("items", {
				"item_code": row.item,
				"qty": flt(row.qty),
				"rate": flt(row.taxable_value) / flt(row.qty) if flt(row.qty) else 0,
				"warehouse": row.source_warehouse,
				"allow_zero_valuation_rate": 1,
				"item_tax_template": item_tax_template
			})

			if igst_account:
				# Clear inital values, if any
				si.taxes = []
				si.append("taxes", {
					"charge_type": "On Net Total",
					"account_head": igst_account,
					"rate": tax_rate,
					"description": f"IGST @ {tax_rate}%"
				})

			# Setting Invoice value, Need to change logic while mutliple items are handling
			si.amazon_invoice_value = flt(si.amazon_invoice_value) + flt(row.invoice_value)
			si.save(ignore_permissions=True)
			frappe.db.set_value(row.doctype, row.name, "sales_invoice", si.name, update_modified=False)
			return
		except Exception as e:
			exception_msg = f"Failed to create Sales Invoice: {str(e)}"
			if row.error_log:
				exception_msg = f"{row.error_log}\n{exception_msg}"
			frappe.db.set_value(row.doctype, row.name, "error_log", exception_msg, update_modified=False)
			self.add_error_log(row, exception_msg)

	def create_purchase_invoice(self, row):
		"""
			Create Purchase Invoice for Target Company
		"""
		if row.source_company == row.target_company:
			return

		existing_invoice = frappe.db.exists("Purchase Invoice", {
			"amazon_invoice_id": row.invoice_number,
			"company": row.target_company,
			"docstatus": ["!=", 2]  # Exclude cancelled invoices
		})

		tax_rate = flt(row.igst_rate)*100
		igst_account = frappe.db.get_value("GST Account", {
			"company": row.target_company,
			"account_type": "Input",
			"parent": "GST Settings",
			"parentfield": "gst_accounts"
		}, "igst_account")

		item_tax_template = frappe.db.get_value("Item Tax Template",{
			"company": row.target_company,
			"gst_rate": tax_rate,
			"disabled":0
		}) or ""

		if existing_invoice:
			try:
				pi = frappe.get_doc("Purchase Invoice", existing_invoice)
				if pi.docstatus == 1:
					frappe.db.set_value(row.doctype, row.name, "purchase_invoice", existing_invoice, update_modified=False)
					return

				# Handling Bundle Items
				if frappe.db.get_value('Item', row.item, 'is_bundle_item'):
					bundle = {
						"item_code": row.item,
						"qty": flt(row.qty),
						"stock_qty": flt(row.qty),
						"uom": frappe.db.get_value('Item', row.item, 'stock_uom'),
						"rate": flt(row.taxable_value) / flt(row.qty) if flt(row.qty) else 0,
						"base_rate": flt(row.taxable_value) / flt(row.qty) if flt(row.qty) else 0,
						"amount": flt(row.taxable_value),
						"base_amount": flt(row.taxable_value),
						"warehouse": row.target_warehouse,
						"item_tax_template": item_tax_template
					}
					pi.append("bundle_items", bundle)
					bundle_row = pi.bundle_items[-1]
					populated_items = get_items_from_bundle(bundle_row.as_dict())
					for populated_item in populated_items:
						pi.append("items", populated_item)
				else:
					#Handling non bundled items
					pi.append("items", {
						"item_code": row.item,
						"qty": flt(row.qty),
						"rate": flt(row.taxable_value) / flt(row.qty) if flt(row.qty) else 0,
						"warehouse": row.target_warehouse,
						"item_tax_template": item_tax_template
					})

				pi.amazon_invoice_value = flt(pi.amazon_invoice_value) + flt(row.invoice_value)
				pi.set_missing_values()
				pi.save(ignore_permissions=True)
				frappe.db.set_value(row.doctype, row.name, "purchase_invoice", existing_invoice, update_modified=False)
				return
			except Exception as e:
				exception_msg = f"Failed to update Purchase Invoice: {existing_invoice} - {str(e)}"
				if row.error_log:
					exception_msg = f"{row.error_log}\n{exception_msg}"
				frappe.db.set_value(row.doctype, row.name, "error_log", exception_msg, update_modified=False)
				self.add_error_log(row, exception_msg)
				return

		supplier = frappe.db.get_value("Supplier", {"represents_company": row.source_company, "is_internal_supplier": 1})

		if not supplier:
			self.add_error_log(row, f"Internal Supplier for Company {row.source_company} not found.")
			return

		try:
			pi = frappe.new_doc("Purchase Invoice")
			pi.supplier = supplier
			pi.company = row.target_company
			pi.posting_date = row.invoice_date
			pi.posting_time = row.invoice_time
			pi.set_posting_time = 1
			pi.bill_no = row.invoice_number
			pi.update_stock = 1
			pi.amazon_invoice_id = row.invoice_number
			pi.set_warehouse = row.target_warehouse
			pi.disable_rounded_total = 1
			price_list = frappe.db.get_single_value("eSeller Settings", "inter_company_price_list")
			if price_list:
				pi.buying_price_list = price_list

			#Adding bundle Items to Bundle Items table
			if frappe.db.get_value('Item', row.item, 'is_bundle_item'):
				bundle = {
					"item_code": row.item,
					"qty": flt(row.qty),
					"stock_qty": flt(row.qty),
					"uom": frappe.db.get_value('Item', row.item, 'stock_uom'),
					"rate": flt(row.taxable_value) / flt(row.qty) if flt(row.qty) else 0,
					"base_rate": flt(row.taxable_value) / flt(row.qty) if flt(row.qty) else 0,
					"amount": flt(row.taxable_value),
					"base_amount": flt(row.taxable_value),
					"warehouse": row.target_warehouse,
					"item_tax_template": item_tax_template
				}
				pi.append("bundle_items", bundle)
				populated_items = get_items_from_bundle(bundle)
				for populated_item in populated_items:
					pi.append("items", populated_item)
			else:
				#Handling non bundled Itesm
				pi.append("items", {
					"item_code": row.item,
					"qty": flt(row.qty),
					"rate": flt(row.taxable_value) / flt(row.qty) if flt(row.qty) else 0,
					"warehouse": row.target_warehouse,
					"item_tax_template": item_tax_template
				})

			if igst_account:
				#Clear table initially
				pi.taxes = []
				pi.append("taxes", {
						"charge_type": "On Net Total",
						"account_head": igst_account,
						"rate": tax_rate,
						"description": f"IGST @ {tax_rate}%"
					})

			# Setting Invoice value, Need to change logic while mutliple items are handling
			pi.amazon_invoice_value = flt(pi.amazon_invoice_value) + flt(row.invoice_value)
			pi.set_missing_values()
			pi.save(ignore_permissions=True)
			frappe.db.set_value(row.doctype, row.name, "purchase_invoice", pi.name, update_modified=False)
		except Exception as e:
			exception_msg = f"Failed to create Purchase Invoice: {str(e)}"
			if row.error_log:
				exception_msg = f"{row.error_log}\n{exception_msg}"
			frappe.db.set_value(row.doctype, row.name, "error_log", exception_msg, update_modified=False)
			self.add_error_log(row, exception_msg)

	def check_duplicate_stn_entry(self, row):
		"""
			Check if the row is already processed in any other STN entry.
			Match by Invoice Number, ASIN, Transaction Type, and Transaction ID.
			If duplicate exists, set transaction references and mark transactions_created as 1.
		"""
		duplicate_row = frappe.db.get_value("Amazon STN Entry Item", {
			"invoice_number": row.invoice_number,
			"asin": row.asin,
			"transaction_type": row.transaction_type,
			"transaction_id": row.transaction_id,
			"transactions_created": 1,
			"name": ["!=", row.name]
		}, ["name", "sales_invoice", "purchase_invoice", "stock_entry"], as_dict=True)

		if duplicate_row:
			row.sales_invoice = duplicate_row.sales_invoice
			row.purchase_invoice = duplicate_row.purchase_invoice
			row.stock_entry = duplicate_row.stock_entry
			row.ready_to_process = 1
			row.transactions_created = 1
			row.error_log = f"Duplicate row detected. References copied from {duplicate_row.name}."

	def build_batch_job_chain(self, batch_size=100):
		rows_to_process = [
			r.name for r in self.stn_entries
			if r.ready_to_process and not r.transactions_created
		]
		total = len(rows_to_process)
		if not total:
			emit_stn_progress(self.name, {"stage": "complete", "message": "Nothing to process"})
			return

		batches = [rows_to_process[i:i + batch_size] for i in range(0, total, batch_size)]
		total_batches = len(batches)

		site = frappe.local.site
		user = frappe.session.user
		queue = get_queue("long")

		prev_job = None
		for idx, row_names in enumerate(batches):
			prev_job = queue.enqueue_call(
				execute_job,
				timeout=6000,
				kwargs=dict(
					site=site,
					method="eseller_suite.eseller_suite.doctype.amazon_stn_entry.amazon_stn_entry.process_stn_batch",
					event=None,
					job_name=f"stn_batch_{self.name}_{idx}",
					kwargs=dict(
						docname=self.name,
						row_names=row_names,
						batch_index=idx,
						total_batches=total_batches,
						user=user,
					),
					is_async=True,
					retry=0,
				),
				depends_on=prev_job,  # None for the first batch → runs immediately
			)

		# Final job only runs once every batch job has finished successfully
		queue.enqueue_call(
			execute_job,
			timeout=1200,
			kwargs=dict(
				site=site,
				method="eseller_suite.eseller_suite.doctype.amazon_stn_entry.amazon_stn_entry.process_stn_submit",
				event=None,
				job_name=f"stn_submit_{self.name}",
				kwargs=dict(docname=self.name),
				is_async=True,
				retry=0,
			),
			depends_on=prev_job,
		)

		emit_stn_progress(
			self.name,
			{
				"stage": "queued",
				"total_batches": total_batches,
				"message": f"Queued {total_batches} batch job(s)",
			},
			user=user,
		)

	def _process_single_stn_row(self, stn_row, stock_entry_type):
		if stn_row.transaction_type == 'FC_REMOVAL-Cancel':
			self.handle_fc_removal_cancel(stn_row)
			return

		if stn_row.transaction_type == 'EInvoiceCancel':
			self.handle_e_invoice_cancel(stn_row)
			return

		if stn_row.source_company == stn_row.target_company:
			self.create_stock_entries_for_material_transfer(stn_row, stock_entry_type)
		else:
			self.create_sales_invoice(stn_row)
			self.create_purchase_invoice(stn_row)
			frappe.db.set_value(stn_row.doctype, stn_row.name, "transactions_created", 1, update_modified=False)

	@frappe.whitelist()
	def retry_fetching_data(self):
		"""
			Fetch missing Company, Warehouse, Item etc.
		"""
		updated = False

		for row in self.stn_entries:
			if not row.ready_to_process:
				updated = True
				row.error_log = ""
				self.map_companies_and_warehouses(row)
				self.map_and_update_item(row)
				self.set_ready_to_process(row)

		if updated:
			self.save()
			frappe.msgprint("Updated Successfully", alert=True, indicator="green")
			return 1

		frappe.msgprint("Nothing to Update", alert=True, indicator="orange")
		return 0

def get_items_from_bundle(bundle):
	price_list = frappe.db.get_single_value("eSeller Settings", "inter_company_price_list") or ""
	populated_items = []
	children = get_bundle_items(bundle.get('item_code')) or []
	has_multiple_items = len(children) > 1
	for child in children:
		item_code = child.get("item_code")
		qty = flt(child.get("qty")) * flt(bundle.get('qty'))
		rate = flt(bundle.get("rate")) / flt(child.get("qty"))
		conversion_factor = 1
		stock_uom = child["uom"]
		if has_multiple_items:
			#Individual Items amount should be based on Item Price, else seting as 0
			rate = 0
			if price_list:
				item_price = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": price_list}, "price_list_rate") or 0
				if item_price:
					rate = item_price
		populated_items.append({
			"item_code": item_code,
			"item_name": child["item_name"],
			"qty": qty,
			"uom": child["uom"],
			"stock_uom": stock_uom,
			"conversion_factor": conversion_factor,
			"rate": rate,
			"amount": qty * rate,
			"base_rate": rate,
			"base_amount": qty * rate,
			"description": child.get("description"),
			"warehouse": bundle.get('warehouse'),
			"bundle_parent": bundle.get('name'),
			"bundle_qty": flt(child.get("qty")),
			"item_tax_rate": '{}',
			"taxable_value": qty * rate,
			"from_bundle_item": 1,
			"item_tax_template": bundle.get('item_tax_template')
		})
	return populated_items

def submit_transactions(stn_entry_name, user=None):
	'''
		Method to submit created Sales, Purchase and Stock entries...
	'''
	if not frappe.db.exists('Amazon STN Entry', stn_entry_name):
		return

	self = frappe.get_doc('Amazon STN Entry', stn_entry_name)

	rows_to_submit = [r for r in self.stn_entries if r.transactions_created]
	total = len(rows_to_submit)

	if not total:
		emit_stn_progress(
			stn_entry_name,
			{"stage": "submitting_entries", "current": 0, "total": 0, "message": "Nothing to submit"},
			user=user,
		)
		return

	for idx, stn_row in enumerate(rows_to_submit, start=1):

		# Submiting Stock Entry with exception handling
		if stn_row.stock_entry:
			emit_stn_progress(
				stn_entry_name,
				{
					"stage": "submitting_entries",
					"current": idx - 1,
					"total": total,
					"message": f"Submitting Stock Entry: {stn_row.stock_entry}",
				},
				user=user,
			)
			frappe.db.savepoint("before_se_submit")
			try:
				se_doc = frappe.get_doc('Stock Entry', stn_row.stock_entry)
				if se_doc.docstatus == 0:
					se_doc.save(ignore_permissions=True)
					se_doc.submit()
			except Exception as e:
				frappe.db.rollback(save_point="before_se_submit")
				exception_msg = f"Failed to submit Stock Entry: {stn_row.stock_entry} - {str(e)}"
				if stn_row.error_log:
					exception_msg = f"{stn_row.error_log}\n{exception_msg}"
				frappe.db.set_value(stn_row.doctype, stn_row.name, "error_log", exception_msg, update_modified=False)
				emit_stn_progress(
					stn_entry_name,
					{
						"stage": "submitting_entries",
						"current": idx,
						"total": total,
						"message": f"Failed: Stock Entry: {stn_row.stock_entry}",
					},
					user=user,
				)

		# Submiting Sales Invoice with exception handling
		if stn_row.sales_invoice:
			if stn_row.purchase_invoice:
				frappe.db.set_value('Sales Invoice', stn_row.sales_invoice, "inter_company_invoice_reference", stn_row.purchase_invoice, update_modified=False)
			emit_stn_progress(
				stn_entry_name,
				{
					"stage": "submitting_entries",
					"current": idx - 1,
					"total": total,
					"message": f"Submitting Sales Invoice: {stn_row.sales_invoice}",
				},
				user=user,
			)
			frappe.db.savepoint("before_si_submit")
			try:
				si_doc = frappe.get_doc('Sales Invoice', stn_row.sales_invoice)
				if si_doc.docstatus == 0:
					si_doc.discount_amount = 0 #To trigger discount calculation
					if stn_row.purchase_invoice:
						si_doc.inter_company_invoice_reference = stn_row.purchase_invoice
					si_doc.save(ignore_permissions=True)
					si_doc.submit()
			except Exception as e:
				frappe.db.rollback(save_point="before_si_submit")
				exception_msg = f"Failed to submit Sales Invoice: {stn_row.sales_invoice} - {str(e)}"
				if stn_row.error_log:
					exception_msg = f"{stn_row.error_log}\n{exception_msg}"
				frappe.db.set_value(stn_row.doctype, stn_row.name, "error_log", exception_msg, update_modified=False)
				emit_stn_progress(
					stn_entry_name,
					{
						"stage": "submitting_entries",
						"current": idx,
						"total": total,
						"message": f"Failed: Sales Invoice: {stn_row.sales_invoice}",
					},
					user=user,
				)

		# Submiting Purchase Invoice with exception handling
		if stn_row.purchase_invoice:
			if stn_row.sales_invoice:
				frappe.db.set_value('Purchase Invoice', stn_row.purchase_invoice, "inter_company_invoice_reference", stn_row.sales_invoice, update_modified=False)
			emit_stn_progress(
				stn_entry_name,
				{
					"stage": "submitting_entries",
					"current": idx - 1,
					"total": total,
					"message": f"Submitting Purchase Invoice: {stn_row.purchase_invoice}",
				},
				user=user,
			)
			frappe.db.savepoint("before_pi_submit")
			try:
				pi_doc = frappe.get_doc('Purchase Invoice', stn_row.purchase_invoice)
				if pi_doc.docstatus == 0:
					pi_doc.discount_amount = 0 #To trigger discount calculation
					pi_doc.save(ignore_permissions=True)
					pi_doc.submit()
			except Exception as e:
				frappe.db.rollback(save_point="before_pi_submit")
				exception_msg = f"Failed to submit Purchase Invoice: {stn_row.purchase_invoice} - {str(e)}"
				if stn_row.error_log:
					exception_msg = f"{stn_row.error_log}\n{exception_msg}"
				frappe.db.set_value(stn_row.doctype, stn_row.name, "error_log", exception_msg, update_modified=False)
				emit_stn_progress(
					stn_entry_name,
					{
						"stage": "submitting_entries",
						"current": idx,
						"total": total,
						"message": f"Failed: Purchase Invoice: {stn_row.purchase_invoice}",
					},
					user=user,
				)

		# Row done — report progress at row granularity regardless of which sub-docs it had
		emit_stn_progress(
			stn_entry_name,
			{
				"stage": "submitting_entries",
				"current": idx,
				"total": total,
				"message": f"Processed row {idx}/{total} (Row {stn_row.idx})",
			},
			user=user,
		)

	emit_stn_progress(
		stn_entry_name,
		{"stage": "submitting_entries", "current": total, "total": total, "message": "All transactions submitted"},
		user=user,
	)
	frappe.db.set_value('Amazon STN Entry', stn_entry_name, 'processing_completed', 1, update_modified=False)

@frappe.whitelist()
def export_stock_exceptions(stn_entry_name):
	if not frappe.db.exists('Amazon STN Entry', stn_entry_name):
		frappe.throw(f"Amazon STN Entry '{stn_entry_name}' does not exist.")
		return

	doc = frappe.get_doc("Amazon STN Entry", stn_entry_name)
	html_content = doc.get_error_message_html()
	if not html_content:
		frappe.throw("No exception log found to parse")

	# Strip HTML tags but keep the link text (Item/Warehouse names)
	soup = BeautifulSoup(html_content, "html.parser")
	for br in soup.find_all("br"):
		br.replace_with("\n")
	text = soup.get_text()

	pattern = re.compile(
		r"Row\s+\d+:\s*(?P<exception>.*?):\s*"
		r"(?P<txn>[\w\-]+)\s*-\s*(?P<qty>[\d.]+)\s*units of\s*"
		r"Item\s+(?P<item>[\w\-]+):.*?needed in\s+"
		r"Warehouse\s+(?P<warehouse>.*?)\s+to complete this transaction",
		re.IGNORECASE
	)

	rows = [["Exception", "Transaction ID", "Item", "Qty", "Warehouse"]]
	for m in pattern.finditer(text):
		rows.append([
			m.group("exception").strip(),
			m.group("txn").strip(),
			m.group("item").strip(),
			float(m.group("qty")),
			m.group("warehouse").strip(),
		])

	if len(rows) == 1:
		frappe.throw("No stock exceptions could be parsed from the log")

	xlsx_file = make_xlsx(rows, "Stock Exceptions")

	frappe.response["filename"] = "stock_exceptions_{0}.xlsx".format(stn_entry_name)
	frappe.response["filecontent"] = xlsx_file.getvalue()
	frappe.response["type"] = "binary"

def queue_stn_batch_chain(docname, batch_size=100):
	doc = frappe.get_doc("Amazon STN Entry", docname)
	doc.build_batch_job_chain(batch_size)

def process_stn_submit(docname, user=None):
	submit_transactions(docname)
	emit_stn_progress(docname, {"stage": "complete", "message": "All transactions submitted"}, user=user)


def process_stn_batch(docname, row_names, batch_index, total_batches, user=None):
	doc = frappe.get_doc("Amazon STN Entry", docname)
	stock_entry_type = frappe.db.get_value("Stock Entry Type", {"purpose": "Material Transfer"}, "name")
	if not stock_entry_type:
		frappe.throw(_("Stock Entry Type for Material Transfer not found. Please configure it before submitting the STN Entry."))

	rows = [r for r in doc.stn_entries if r.name in row_names]
	total_rows = len(rows)

	def emit(current, message, stage="creating_entries"):
		emit_stn_progress(
			docname,
			{
				"stage": stage,
				"batch_index": batch_index,
				"total_batches": total_batches,
				"current_row": current,
				"total_rows_in_batch": total_rows,
				"message": message,
			},
			user=user,
		)

	for idx, stn_row in enumerate(rows, start=1):
		emit(idx, f"Processing {stn_row.transaction_type} for Row {stn_row.idx}")
		try:
			doc._process_single_stn_row(stn_row, stock_entry_type)
		except Exception:
			frappe.log_error(
				title=f"STN row failed: Row {stn_row.idx}",
				message=frappe.get_traceback(),
			)
			frappe.db.rollback()
			emit(idx, f"Failed: Row {stn_row.idx}", stage="row_failed")
			continue

	frappe.db.commit()
	emit(total_rows, f"Batch {batch_index + 1}/{total_batches} done", stage="batch_complete")


def emit_stn_progress(docname, payload, user=None):
	payload = {
		**payload,
		"docname": docname,
		"timestamp": frappe.utils.now_datetime().isoformat(),
	}
	frappe.cache().set_value(f"stn_progress:{docname}", payload, expires_in_sec=3600)
	frappe.publish_realtime(event="stn_processing_progress", message=payload, user=user)


@frappe.whitelist()
def get_stn_progress(docname):
	frappe.has_permission("Amazon STN Entry", "read", throw=True)
	return frappe.cache().get_value(f"stn_progress:{docname}")
