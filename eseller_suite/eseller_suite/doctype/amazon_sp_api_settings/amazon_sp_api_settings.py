# Copyright (c) 2024, efeone and contributors
# For license information, please see license.txt


from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, now_datetime, today, get_date_str

from erpnext.stock.utils import get_stock_balance

import pytz


def enhance_hsn_error_with_items(error_message, doc):
	"""Enhance HSN/SAC validation errors with item information"""
	if not doc or not hasattr(doc, 'items'):
		return error_message
	
	# Check if error is related to HSN/SAC
	hsn_keywords = ["HSN/SAC", "HSN", "SAC", "hsn_code", "gst_hsn_code"]
	if not any(keyword.lower() in str(error_message).lower() for keyword in hsn_keywords):
		return error_message
	
	import re
	error_str = str(error_message)
	
	# Check for specific error format: "HSN/SAC must exist and should be X digits long for the following row numbers:"
	# Pattern matches: "for the following row numbers:" followed by optional HTML tags and row numbers
	row_numbers_pattern = r'(for the following row numbers:\s*(?:<br>)?\s*)(?:<[^>]+>)?([\d\s,]+)(?:<[^>]+>)?'
	match = re.search(row_numbers_pattern, error_str, re.IGNORECASE)
	
	if match:
		# Extract row numbers (remove HTML tags)
		row_numbers_str = match.group(2).strip()
		row_numbers_str = re.sub(r'<[^>]+>', '', row_numbers_str)
		row_numbers = [int(r.strip()) for r in row_numbers_str.split(',') if r.strip().isdigit()]
		
		# Create a mapping of row idx to item_name
		row_to_item = {}
		for item in doc.items:
			if item.idx in row_numbers:
				item_name = item.get('item_name') or item.get('item_code') or 'Unknown'
				row_to_item[item.idx] = item_name
		
		# Replace row numbers with "row_number {item_name}"
		enhanced_rows = []
		for row_num in row_numbers:
			if row_num in row_to_item:
				enhanced_rows.append(f"{row_num} {row_to_item[row_num]}")
			else:
				enhanced_rows.append(str(row_num))
		
		# Replace the row numbers in the error message, preserving the prefix
		enhanced_rows_str = ", ".join(enhanced_rows)
		# Use a lambda function to avoid regex group reference issues
		enhanced_message = re.sub(
			row_numbers_pattern,
			lambda m: m.group(1) + enhanced_rows_str,
			error_str,
			flags=re.IGNORECASE
		)
		return enhanced_message
	
	# Fallback: Find items without HSN code
	items_without_hsn = []
	for item in doc.items:
		if not item.get("gst_hsn_code"):
			item_info = f"Row {item.idx}: {item.get('item_code', 'Unknown')} ({item.get('item_name', 'N/A')})"
			items_without_hsn.append(item_info)
	
	if items_without_hsn:
		items_list = "\n".join(items_without_hsn)
		enhanced_message = f"{error_message}\n\nItems requiring HSN/SAC Code:\n{items_list}"
		return enhanced_message
	
	return error_message


class AmazonSPAPISettings(Document):
	def validate(self):
		self.validate_after_date()

		if self.is_active == 0:
			self.enable_sync = 0

		if not self.max_retry_limit:
			self.max_retry_limit = 1
		elif self.max_retry_limit and self.max_retry_limit > 5:
			frappe.throw(frappe._("Value for <b>Max Retry Limit</b> must be less than or equal to 5."))

	def save(self):
		super(AmazonSPAPISettings, self).save()

	def validate_after_date(self):
		if getdate(self.after_date) > getdate(today()):
			frappe.throw(_("The date cannot be in the future."))
		if getdate(self.after_date) < add_days(getdate(today()), -60):
			self.enable_sync = 0
			self.sync_selected_date_only = 1
			frappe.msgprint(
				_("As the date is more than 60 days in the past, sync has been disabled and 'Sync Selected Date Only' has been enabled. Please update the date or enable sync if you want to fetch data for this date."),
				indicator="orange",
				alert=True
			)

	@frappe.whitelist()
	def get_order_details(self):
		from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_repository import (
			get_orders,
		)
		if self.is_active == 1:
			job_name = f"Get Amazon Orders - {self.name}"

			if frappe.db.get_all("RQ Job", {"job_name": job_name, "status": ["in", ["queued", "started"]]}):
				return frappe.msgprint(_("The order details are currently being fetched in the background."))

			frappe.enqueue(
				job_name=job_name,
				method=get_orders,
				amz_setting_name=self.name,
				last_updated_after=self.after_date,
				sync_selected_date_only=self.sync_selected_date_only,
				timeout=6000,
				now=frappe.flags.in_test,
			)

			frappe.msgprint(_("Order details will be fetched in the background."))
		else:
			frappe.msgprint(
				_("Please enable the Amazon SP API Settings {0}.").format(frappe.bold(self.name))
			)

	@frappe.whitelist()
	def fetch_warehouses(self):
		"""Fetch warehouses from Amazon Supply Sources and create them in ERPNext if they don't exist"""
		if not self.is_active:
			frappe.throw(_("Please enable the Amazon SP API Settings first."))
		
		from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api import (
			SupplySources,
		)
		
		try:
			# Initialize SupplySources API
			supply_sources_api = SupplySources(
				client_id=self.client_id,
				client_secret=self.get_password("client_secret"),
				refresh_token=self.refresh_token,
				country_code=self.country,
			)
			
			# Fetch supply sources from Amazon
			# Note: This endpoint may return 403 if not available or if permissions are not granted
			try:
				all_supply_sources = []
				next_page_token = None
				
				# Fetch all pages of supply sources
				while True:
					supply_sources_response = supply_sources_api.get_supply_sources(
						page_size=100,
						next_page_token=next_page_token
					)
					
					# Parse supply sources response - response structure: {"supplySources": [...], "nextPageToken": "..."}
					if isinstance(supply_sources_response, dict):
						page_supply_sources = supply_sources_response.get("supplySources", [])
						all_supply_sources.extend(page_supply_sources)
						
						# Check for next page
						next_page_token = supply_sources_response.get("nextPageToken")
						if not next_page_token:
							break
					else:
						break
				
				if not all_supply_sources:
					frappe.msgprint(_("No supply sources found in the response from Amazon."))
					return {"status": "no_data", "message": "No supply sources found in the response."}
				
			except Exception as api_error:
				# If the endpoint fails, log error and return
				error_details = str(api_error)
				frappe.log_error(
					title="Supply Sources API Error",
					message=f"The Supply Sources API endpoint returned an error: {error_details}\n\n"
							f"Error: {error_details}\n"
							f"Traceback: {frappe.get_traceback()}"
				)
				if "403" in error_details or "404" in error_details:
					frappe.throw(
						_("The Supply Sources API endpoint is not available (403/404). "
						  "Please check your IAM role permissions for the Supply Sources API, "
						  "or this endpoint may not be available for your marketplace. "
						  "Check the error log for more details.")
					)
				else:
					raise
			
			# Create warehouses from supply sources
			created_count = 0
			skipped_count = 0
			errors = []
			
			# Create warehouses in ERPNext from supply sources
			for source in all_supply_sources:
				try:
					# Extract warehouse information from supply source
					supply_source_id = source.get("supplySourceId")
					supply_source_code = source.get("supplySourceCode")
					supply_source_alias = source.get("alias")
					
					# Use supplySourceCode as warehouse code, fallback to supplySourceId
					warehouse_code = supply_source_code or supply_source_id
					
					# Use alias as warehouse name, fallback to code or ID
					warehouse_name = supply_source_alias or supply_source_code or supply_source_id
					
					# Get supply source data
					supply_source_data = source
					
					if not warehouse_code:
						errors.append(f"Missing warehouse code in center data: {source}")
						continue
					
					# Check if warehouse already exists
					if frappe.db.exists("Warehouse", warehouse_code):
						skipped_count += 1
						continue
					
					# Create new warehouse
					warehouse = frappe.new_doc("Warehouse")
					warehouse.warehouse_name = warehouse_name
					warehouse.company = self.company
					warehouse.is_group = 0
					
					# Set parent warehouse if specified in settings
					if hasattr(self, 'parent_item_group') and self.parent_item_group:
						# Try to find a parent warehouse with the same name pattern
						parent_warehouse = frappe.db.get_value("Warehouse", {"warehouse_name": "All Warehouses", "is_group": 1}, "name")
						if parent_warehouse:
							warehouse.parent_warehouse = parent_warehouse
					
					# Add custom field for Amazon supply source ID if it exists
					if hasattr(warehouse, 'amazon_supply_source_id'):
						warehouse.amazon_supply_source_id = supply_source_id
					if hasattr(warehouse, 'amazon_supply_source_code'):
						warehouse.amazon_supply_source_code = supply_source_code
					
					# Add supply source information to warehouse
					if supply_source_id and supply_source_data:
						# Try to add to custom field if it exists
						if hasattr(warehouse, 'amazon_supply_source_id'):
							warehouse.amazon_supply_source_id = supply_source_id
						
						# Get supply source details
						supply_source_alias = supply_source_data.get("alias") or ""
						supply_source_code = supply_source_data.get("supplySourceCode") or ""
						
						# Enhance warehouse name with supply source information
						supply_source_display = supply_source_alias or supply_source_code or supply_source_id
						if supply_source_display and supply_source_display not in warehouse_name:
							warehouse.warehouse_name = f"{warehouse_name} - {supply_source_display}"
						
						# Store address information if available (could be used for warehouse address)
						supply_source_address = supply_source_data.get("address", {})
						if supply_source_address and hasattr(warehouse, 'custom_amazon_supply_source_address'):
							warehouse.custom_amazon_supply_source_address = frappe.as_json(supply_source_address)
					elif supply_source_id:
						# If we only have the ID without full data
						if hasattr(warehouse, 'amazon_supply_source_id'):
							warehouse.amazon_supply_source_id = supply_source_id
						if supply_source_id not in warehouse_name:
							warehouse.warehouse_name = f"{warehouse_name} (Supply Source: {supply_source_id})"
					
					warehouse.insert(ignore_permissions=True)
					created_count += 1
					
				except Exception as e:
					error_msg = f"Error creating warehouse {warehouse_code}: {str(e)}"
					errors.append(error_msg)
					frappe.log_error(
						title="Error creating warehouse from Amazon",
						message=f"{error_msg}\nCenter data: {frappe.as_json(source)}\nTraceback: {frappe.get_traceback()}"
					)
			
			# Prepare response message
			message_parts = []
			if created_count > 0:
				message_parts.append(_("{0} warehouse(s) created successfully.").format(created_count))
			if skipped_count > 0:
				message_parts.append(_("{0} warehouse(s) already exist and were skipped.").format(skipped_count))
			if errors:
				message_parts.append(_("{0} error(s) occurred.").format(len(errors)))
			
			message = " ".join(message_parts) if message_parts else _("No warehouses were processed.")
			
			if errors:
				frappe.log_error(
					title="Warehouse fetch errors",
					message="\n".join(errors)
				)
			
			frappe.msgprint(message, indicator="green" if created_count > 0 else "orange")
			
			return {
				"status": "success",
				"created": created_count,
				"skipped": skipped_count,
				"errors": len(errors),
				"message": message
			}
			
		except Exception as e:
			# Handle SPAPIError specifically to get detailed error information
			from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api import (
				SPAPIError,
			)
			from requests.exceptions import HTTPError
			
			error_msg = ""
			if isinstance(e, SPAPIError):
				error_msg = f"Amazon API Error: {e.error}"
				if e.error_description and e.error_description != "-":
					error_msg += f" - {e.error_description}"
			elif isinstance(e, HTTPError):
				# Try to get response details for HTTP errors
				response_text = ""
				if hasattr(e, 'response') and e.response is not None:
					try:
						response_text = e.response.text
					except Exception:
						pass
				
				if "403" in str(e):
					error_msg = "403 Forbidden: The Supply Sources API endpoint may not be available or you may not have the required permissions. "
					error_msg += "Please check:\n1. Your SP API credentials have access to Supply Sources API\n"
					error_msg += "2. The endpoint is correct for your marketplace\n"
					error_msg += "3. Your IAM role has the necessary permissions"
					if response_text:
						error_msg += f"\n\nResponse: {response_text}"
				else:
					error_msg = f"HTTP Error: {str(e)}"
					if response_text:
						error_msg += f"\nResponse: {response_text}"
			else:
				error_msg = f"Error fetching warehouses from Amazon: {str(e)}"
			
			frappe.log_error(
				title="Error fetching warehouses from Amazon",
				message=f"{error_msg}\nTraceback: {frappe.get_traceback()}"
			)
			frappe.throw(_(error_msg))

	def _extract_warehouses_from_orders(self):
		"""Extract fulfillment center/warehouse information and supply source from existing Sales Orders"""
		try:
			# Get distinct fulfillment channels and supply sources from existing Sales Orders
			# Also check Amazon Failed Sync Records for order payloads that might contain supply source info
			fulfillment_channels = frappe.db.sql("""
				SELECT DISTINCT fulfillment_channel, COUNT(*) as order_count
				FROM `tabSales Order`
				WHERE amazon_order_id IS NOT NULL
					AND fulfillment_channel IS NOT NULL
					AND fulfillment_channel != ''
				GROUP BY fulfillment_channel
			""", as_dict=True)
			
			# Try to extract supply source IDs from Amazon Failed Sync Records payloads
			supply_sources = {}
			try:
				failed_records = frappe.db.sql("""
					SELECT DISTINCT payload, amazon_order_id
					FROM `tabAmazon Failed Sync Record`
					WHERE payload IS NOT NULL
						AND payload != ''
					LIMIT 100
				""", as_dict=True)
				
				import json
				for record in failed_records:
					try:
						if record.payload:
							payload = json.loads(record.payload) if isinstance(record.payload, str) else record.payload
							# Check for supply source in various possible locations
							supply_source = (
								payload.get("ActualFulfillmentSupplySourceId") or
								payload.get("FulfillmentSupplySourceId") or
								payload.get("supplySourceId") or
								None
							)
							if supply_source:
								fulfillment_channel = payload.get("FulfillmentChannel") or "UNKNOWN"
								if fulfillment_channel not in supply_sources:
									supply_sources[fulfillment_channel] = set()
								supply_sources[fulfillment_channel].add(supply_source)
					except Exception:
						continue
			except Exception:
				pass
			
			if not fulfillment_channels:
				return {"status": "no_data", "fulfillment_centers": []}
			
			fulfillment_centers = []
			for channel in fulfillment_channels:
				# Get supply source for this fulfillment channel if available
				channel_supply_sources = supply_sources.get(channel.fulfillment_channel, set())
				supply_source_id = list(channel_supply_sources)[0] if channel_supply_sources else None
				
				# Create a warehouse entry based on fulfillment channel
				# AFN = Amazon Fulfillment Network (FBA)
				# MFN = Merchant Fulfillment Network (FBM)
				if channel.fulfillment_channel == "AFN":
					# Use the AFN warehouse from settings if available
					if hasattr(self, 'afn_warehouse') and self.afn_warehouse:
						center_data = {
							"fulfillmentCenterId": "AFN",
							"fulfillmentCenterName": f"Amazon Fulfillment Network ({self.afn_warehouse})",
							"code": "AFN"
						}
					else:
						center_data = {
							"fulfillmentCenterId": "AFN",
							"fulfillmentCenterName": "Amazon Fulfillment Network",
							"code": "AFN"
						}
					if supply_source_id:
						center_data["supplySourceId"] = supply_source_id
					fulfillment_centers.append(center_data)
				elif channel.fulfillment_channel == "MFN":
					# Use the MFN warehouse from settings if available
					if hasattr(self, 'warehouse') and self.warehouse:
						center_data = {
							"fulfillmentCenterId": "MFN",
							"fulfillmentCenterName": f"Merchant Fulfillment Network ({self.warehouse})",
							"code": "MFN"
						}
					else:
						center_data = {
							"fulfillmentCenterId": "MFN",
							"fulfillmentCenterName": "Merchant Fulfillment Network",
							"code": "MFN"
						}
					if supply_source_id:
						center_data["supplySourceId"] = supply_source_id
					fulfillment_centers.append(center_data)
			
			return {"status": "success", "fulfillment_centers": fulfillment_centers}
			
		except Exception as e:
			frappe.log_error(
				title="Error extracting warehouses from orders",
				message=f"Error: {str(e)}\nTraceback: {frappe.get_traceback()}"
			)
			return {"status": "error", "fulfillment_centers": []}

	@frappe.whitelist()
	def create_report(self, report_type, from_date=None, to_date=None):
		'''
			Method to create report via SP APIS
			from_date and to_date will be based on IST
			IST to UTC conversion is handled
		'''
		if not from_date:
			from_date = add_days(getdate(), -1)
		if not to_date:
			to_date = today()
		from_date = add_days(getdate(from_date), -1)	# Subtract 1 day for IST to UTC conversion
		to_date = add_days(getdate(to_date), -1)	# Subtract 1 day for IST to UTC conversion
		from_date_str = getdate(from_date).strftime("%Y-%m-%d")
		from_date_str_tz = f"{from_date_str}T18:30:00Z"	# Convert to UTC by subtracting 1 day and setting time to 18:30:00 (which is 00:00:00 IST)
		to_date_str = getdate(to_date).strftime("%Y-%m-%d")
		to_date_str_tz = f"{to_date_str}T18:30:00Z"	# Convert to UTC by subtracting 1 day and setting time to 18:30:00 (which is 00:00:00 IST)
		if not self.is_active:
			frappe.throw(_("Please enable the Amazon SP API Settings first."))

		from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api import (
			ReportAPIs,
		)

		try:
			# Initialize SupplySources API
			report_api = ReportAPIs(
				client_id=self.client_id,
				client_secret=self.get_password("client_secret"),
				refresh_token=self.refresh_token,
				country_code=self.country,
			)

			try:
				reports_response = report_api.create_report(
					report_type=report_type,
					data_start_time=from_date_str_tz,
					data_end_time=to_date_str_tz
				)
				if reports_response and reports_response.get('reportId'):
					create_report_api_log(
						report_id=reports_response['reportId'],
						report_type=report_type,
						from_date=from_date,
						to_date=to_date
					)
				return reports_response

			except Exception as api_error:
				# If the endpoint fails, log error and return
				error_details = str(api_error)
				frappe.log_error(title='Error creating Amazon report', message=f"Error details: {error_details}")
				return None

		except Exception as e:
			error_msg = str(e)
			frappe.log_error(
				title="Error creating Amazon report",
				message=f"Error: {error_msg}"
			)
			return None

# Called via a hook in every hour.
def schedule_get_order_details():
	current_datetime = now_datetime()

	yesterday_23 = (current_datetime - timedelta(days=1)).replace(
		hour=23, minute=0, second=0, microsecond=0
	)
	today_01 = current_datetime.replace(hour=1, minute=0, second=0, microsecond=0)

	if yesterday_23 <= current_datetime < today_01: # Makes it so that the hourly scheduler won't work at midnight and the daily one will
		return

	from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_repository import (
		get_orders,
	)

	system_timezone = frappe.db.get_single_value("System Settings", "time_zone")

	local_tz = pytz.timezone(system_timezone)
	gmt_tz = pytz.timezone("GMT")

	local_datetime = local_tz.localize(current_datetime)
	gmt_datetime = local_datetime.astimezone(gmt_tz)
	current_date = gmt_datetime.strftime("%Y-%m-%d")

	amz_settings = frappe.get_all(
		"Amazon SP API Settings",
		filters={"is_active": 1, "enable_sync": 1},
		fields=["name"],
	)

	for amz_setting in amz_settings:
		get_orders(amz_setting_name=amz_setting.name, last_updated_after=current_date)

# Called via a hook every day to sync data of the previous day.
def schedule_get_order_details_daily():
	from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_repository import (
		get_orders,
	)

	from_date = add_days(getdate(), -1).strftime("%Y-%m-%d")

	amz_settings = frappe.get_all(
		"Amazon SP API Settings",
		filters={"is_active": 1, "enable_sync": 1},
		fields=["name"],
	)

	for amz_setting in amz_settings:
		get_orders(amz_setting_name=amz_setting.name, last_updated_after=from_date)

def enq_si_submit():
	sales_invoices = frappe.db.get_all(
		"Sales Invoice",
		{"docstatus": 0, "amazon_order_id": ["is", "set"]},
		pluck="name"
	)
	for sales_invoice_name in sales_invoices:
		try:
			enq_si_submit_doc(sales_invoice_name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()

def enq_si_submit_doc(sales_invoice):
	sales_invoice_doc = frappe.get_doc("Sales Invoice", sales_invoice)
	allow_negative_stock = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
	if allow_negative_stock:
		sales_invoice_doc.submit()
		return

	# Batch fetch is_stock_item for all items
	all_item_codes = list({
		item.item_code
		for item in [*sales_invoice_doc.items, *sales_invoice_doc.packed_items]
	})
	is_stock_item_map = {
		row.name: row.is_stock_item
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", all_item_codes]},
			fields=["name", "is_stock_item"]
		)
	}

	# Batch fetch stock levels only for stock items
	stock_levels = {
		item.item_code: get_stock_balance(item.item_code, item.warehouse, sales_invoice_doc.posting_date)
		for item in [*sales_invoice_doc.items, *sales_invoice_doc.packed_items]
		if is_stock_item_map.get(item.item_code)
	}

	error_records = []

	for item in sales_invoice_doc.items:
		if not is_stock_item_map.get(item.item_code):
			continue
		stock_qty = stock_levels.get(item.item_code, 0)
		if item.qty > stock_qty:
			error_records.append({
				"doctype": "Amazon Failed Invoice Record",
				"invoice_id": sales_invoice_doc.name,
				"error": (
					f"Insufficient stock for item {item.item_code} as of "
					f"{sales_invoice_doc.posting_date}. Available: {stock_qty}, Required: {item.qty}"
				)
			})

	for item in sales_invoice_doc.packed_items:
		if not is_stock_item_map.get(item.item_code):
			continue
		stock_qty = stock_levels.get(item.item_code, 0)
		if item.qty > stock_qty:
			error_records.append({
				"doctype": "Amazon Failed Invoice Record",
				"invoice_id": sales_invoice_doc.name,
				"error": (
					f"Insufficient stock for item {item.item_code} as of "
					f"{sales_invoice_doc.posting_date}. Available: {stock_qty}, Required: {item.qty}"
				)
			})

	if error_records:
		for record in error_records:
			frappe.get_doc(record).insert(ignore_permissions=True)
		return  # ← don't submit if there are stock errors
	sales_invoice_doc.submit()

def create_report_api_log(report_id, report_type, from_date, to_date):
	'''
		Create a log entry for the created report in Amazon Report API Log doctype.
	'''
	try:
		frappe.get_doc({
			"doctype": "Amazon Report API Log",
			"report_id": report_id,
			"report_type": report_type,
			"data_from_date": getdate(from_date),
			"data_to_date": getdate(to_date)
		}).insert(ignore_permissions=True)
	except Exception as e:
		frappe.log_error(
			title="Failed to create Amazon Report API Log",
			message=f"Report ID: {report_id}\nError: {str(e)}\nTraceback: {frappe.get_traceback()}"
		)

def create_daily_reports_schedule():
	'''
		Scheduler to create reports for the previous day.
	'''
	if not frappe.db.get_single_value("eSeller Settings", "enable_report_scheduler"):
		return
	from_date = add_days(getdate(), -1)
	to_date = getdate()
	amz_settings = frappe.get_all(
		"Amazon SP API Settings",
		filters={"is_active": 1},
		pluck="name",
		limit=1,
	)
	report_types = frappe.db.get_all("Amazon Report Type", pluck="name")
	if not amz_settings:
		frappe.log_error(title="Amazon SP API Settings Not Found", message="No active Amazon SP API Settings found. Report creation skipped.")
		return

	self = frappe.get_doc("Amazon SP API Settings", amz_settings[0])
	for report_type in report_types:
		self.create_report(
			report_type=report_type,
			from_date=from_date,
			to_date=to_date
		)
