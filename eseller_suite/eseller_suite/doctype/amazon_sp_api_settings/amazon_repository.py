# Copyright (c) 2024, efeone and contributors
# For license information, please see license.txt


import json
import time
import re

import dateutil
import frappe
from frappe import _
from datetime import datetime, date, timezone
from eseller_suite.eseller_suite.utils import format_date_time_to_ist

from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api import (
	CatalogItems,
	Finances,
	Orders,
	SPAPIError,
)
from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings import (
	AmazonSPAPISettings,
)
from frappe.utils import getdate, add_days, get_datetime

from eseller_suite.eseller_suite.custom_script.sales_order.sales_order import get_account_head


class AmazonRepository:
	def __init__(self, amz_setting: str | AmazonSPAPISettings) -> None:
		if isinstance(amz_setting, str):
			amz_setting = frappe.get_doc("Amazon SP API Settings", amz_setting)

		self.amz_setting = amz_setting
		self.instance_params = dict(
			client_id=self.amz_setting.client_id,
			client_secret=self.amz_setting.get_password("client_secret"),
			refresh_token=self.amz_setting.refresh_token,
			country_code=self.amz_setting.country,
		)

	def return_as_list(self, input) -> list:
		if isinstance(input, list):
			return input
		else:
			return [input]

	def enhance_hsn_error_with_items(self, error_message, doc):
		"""Enhance HSN/SAC validation errors with item information"""
		if not doc or not hasattr(doc, "items"):
			return error_message

		# Check if error is related to HSN/SAC
		hsn_keywords = ["HSN/SAC", "HSN", "SAC", "hsn_code", "gst_hsn_code"]
		if not any(
			keyword.lower() in str(error_message).lower() for keyword in hsn_keywords
		):
			return error_message

		import re

		error_str = str(error_message)

		# Check for specific error format: "HSN/SAC must exist and should be X digits long for the following row numbers:"
		# Pattern matches: "for the following row numbers:" followed by optional HTML tags and row numbers
		row_numbers_pattern = r"(for the following row numbers:\s*(?:<br>)?\s*)(?:<[^>]+>)?([\d\s,]+)(?:<[^>]+>)?"
		match = re.search(row_numbers_pattern, error_str, re.IGNORECASE)

		if match:
			# Extract row numbers (remove HTML tags)
			row_numbers_str = match.group(2).strip()
			row_numbers_str = re.sub(r"<[^>]+>", "", row_numbers_str)
			row_numbers = [
				int(r.strip())
				for r in row_numbers_str.split(",")
				if r.strip().isdigit()
			]

			# Create a mapping of row idx to item_name
			row_to_item = {}
			for item in doc.items:
				if item.idx in row_numbers:
					item_name = (
						item.get("item_name") or item.get("item_code") or "Unknown"
					)
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
				flags=re.IGNORECASE,
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
			enhanced_message = (
				f"{error_message}\n\nItems requiring HSN/SAC Code:\n{items_list}"
			)
			return enhanced_message

		return error_message

	def call_sp_api_method(self, sp_api_method, **kwargs) -> dict:
		errors = {}
		max_retries = self.amz_setting.max_retry_limit

		for x in range(max_retries):
			try:
				result = sp_api_method(**kwargs)
				# Some APIs return data in "payload" key, others return it directly
				if isinstance(result, dict) and "payload" in result:
					payload = result.get("payload")
				else:
					payload = result

				return payload
			except SPAPIError as e:
				if e.error not in errors:
					errors[e.error] = e.error_description

				time.sleep(1)
				continue
			except Exception as e:
				# Catch all other exceptions (network errors, timeouts, etc.)
				error_key = type(e).__name__
				if error_key not in errors:
					errors[error_key] = str(e)

				time.sleep(1)
				continue

		for error in errors:
			msg = f"<b>Error:</b> {error}<br/><b>Error Description:</b> {errors.get(error)}"
			frappe.msgprint(msg, alert=True, indicator="red")
			frappe.log_error(
				message=f"{error}: {errors.get(error)}",
				title=f'Method "{sp_api_method.__name__}" failed',
			)

		self.amz_setting.enable_sync = 0
		self.amz_setting.save()

		frappe.throw(
			_(
				"Scheduled sync has been temporarily disabled because maximum retries have been exceeded!"
			)
		)

	def get_finances_instance(self) -> Finances:
		return Finances(**self.instance_params)

	def get_account(self, name, company=None) -> str:
		if not company:
			company = self.amz_setting.company
		account_name = frappe.db.get_value(
			"Account", {"account_name": "Amazon {0}".format(name), 'company': company }
		)

		if not account_name:
			new_account = frappe.new_doc("Account")
			new_account.account_name = "Amazon {0}".format(name)
			new_account.company = company
			new_account.parent_account = self.amz_setting.market_place_account_group
			new_account.insert(ignore_permissions=True)
			account_name = new_account.name

		return account_name

	def get_charges_and_fees(self, order_id, company=None) -> dict:
		if not company:
			company = self.amz_setting.company
		finances = self.get_finances_instance()
		financial_events_payload = self.call_sp_api_method(
			sp_api_method=finances.list_financial_events_by_order_id, order_id=order_id
		)

		charges_and_fees = {
			"charges": [],
			"fees": [],
			"tds": [],
			"service_fees": [],
			"principal_amounts": {},
			"additional_discount": 0,
			"item_shipment_details": [],
		}

		if not (
			financial_events_payload
			and len(financial_events_payload.get("FinancialEvents", {}))
		):
			return charges_and_fees

		while True:
			shipment_event_list = financial_events_payload.get(
				"FinancialEvents", {}
			).get("ShipmentEventList", [])
			service_fee_event_list = financial_events_payload.get(
				"FinancialEvents", {}
			).get("ServiceFeeEventList", [])
			next_token = financial_events_payload.get("NextToken")
			principal_amounts = {}
			promotion_discount = 0
			seller_sku = ""
			order_item_id = ""
			for shipment_event in shipment_event_list:
				if shipment_event:
					for shipment_item in shipment_event.get("ShipmentItemList", []):
						promotion_list = shipment_item.get("PromotionList", [])
						seller_sku = shipment_item.get("SellerSKU")
						order_item_id = shipment_item.get("OrderItemId", "")
						qty = shipment_item.get("QuantityShipped")
						charges = shipment_item.get("ItemChargeList", [])
						fees = shipment_item.get("ItemFeeList", [])
						tds_list = shipment_item.get("ItemTaxWithheldList", [])
						tdss = []
						if tds_list:
							tdss = tds_list[0].get("TaxesWithheld", [])

						shipment_log_id = '{0}-{1}-{2}'.format(seller_sku, qty, generate_random_string(4))

						for charge in charges:
							charge_type = charge.get("ChargeType")
							amount = charge.get("ChargeAmount", {}).get(
								"CurrencyAmount", 0
							)

							if charge_type != "Principal" and float(amount) != 0:
								charge_account = self.get_account(charge_type, company)
								charges_and_fees.get("charges").append(
									{
										"charge_type": "Actual",
										"account_head": charge_account,
										"tax_amount": amount,
										"description": f"{charge_type} for {seller_sku if seller_sku else order_id}",
										"amazon_order_item_id":order_item_id,
										"shipment_log_id": shipment_log_id
									}
								)
							if charge_type == "Principal":
								principal_amounts[seller_sku] = round(
									(float(amount) / qty), 2
								)

						for fee in fees:
							fee_type = fee.get("FeeType")
							amount = fee.get("FeeAmount", {}).get("CurrencyAmount", 0)

							if float(amount) != 0:
								fee_account = self.get_account(fee_type, company)
								charges_and_fees.get("fees").append(
									{
										"charge_type": "Actual",
										"account_head": fee_account,
										"tax_amount": amount,
										"description": f"{fee_type} for {seller_sku if seller_sku else order_id}",
										"amazon_order_item_id":order_item_id,
										"shipment_log_id": shipment_log_id
									}
								)

						for tds in tdss:
							tds_type = tds.get("ChargeType")
							amount = tds.get("ChargeAmount", {}).get(
								"CurrencyAmount", 0
							)
							if float(amount) != 0:
								tds_account = self.get_account(tds_type, company)
								charges_and_fees.get("tds").append(
									{
										"charge_type": "Actual",
										"account_head": tds_account,
										"tax_amount": amount,
										"description": f"{tds_type} for {seller_sku if seller_sku else order_id}",
										"amazon_order_item_id":order_item_id,
										"shipment_log_id": shipment_log_id
									}
								)

						item_promotion_discount = 0
						for promotion in promotion_list:
							amount = promotion.get("PromotionAmount", {}).get(
								"CurrencyAmount", 0
							)
							promotion_discount += float(amount)
							item_promotion_discount += float(amount)

						charges_and_fees.get("item_shipment_details").append({
							"seller_sku": seller_sku,
							"amazon_order_item_id": order_item_id,
							"qty": qty,
							"shipment_log_id": shipment_log_id,
							"promotion_discount": abs(item_promotion_discount),
						})

			charges_and_fees["principal_amounts"] = principal_amounts
			charges_and_fees["additional_discount"] = promotion_discount

			for service_fee in service_fee_event_list:
				if service_fee:
					for service_fee_item in service_fee.get("FeeList", []):
						fee_type = service_fee_item.get("FeeType")
						amount = service_fee_item.get("FeeAmount", {}).get(
							"CurrencyAmount", 0
						)
						if float(amount) != 0:
							fee_account = self.get_account(fee_type, company)
							charges_and_fees.get("service_fees").append(
								{
									"charge_type": "Actual",
									"account_head": fee_account,
									"tax_amount": amount,
									"description": f"{fee_type} for {seller_sku if seller_sku else order_id}",
									"amazon_order_item_id":order_item_id,
									"shipment_log_id": shipment_log_id
								}
							)

			if not next_token:
				break

			financial_events_payload = self.call_sp_api_method(
				sp_api_method=finances.list_financial_events_by_order_id,
				order_id=order_id,
				next_token=next_token,
			)

		return charges_and_fees

	def get_orders_instance(self) -> Orders:
		return Orders(**self.instance_params)

	def create_item(self, order_item, order_id, default_stock_uom=None) -> str:
		if not default_stock_uom:
			default_stock_uom = frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos"

		def get_item_data(amazon_item):
			"""Extract item data from either AttributeSets (old format) or summaries (new format)"""
			if not amazon_item:
				return {}

			# Try new format first (summaries)
			if (
				amazon_item.get("summaries")
				and len(amazon_item.get("summaries", [])) > 0
			):
				return amazon_item.get("summaries")[0]
			# Fall back to old format (AttributeSets)
			elif (
				amazon_item.get("AttributeSets")
				and len(amazon_item.get("AttributeSets", [])) > 0
			):
				return amazon_item.get("AttributeSets")[0]
			return {}

		def create_item_group(amazon_item) -> str:
			if not amazon_item:
				return self.amz_setting.parent_item_group

			item_data = get_item_data(amazon_item)
			if not item_data:
				return self.amz_setting.parent_item_group

			# New format uses browseClassification.displayName, old format uses ProductGroup
			item_group_name = item_data.get("ProductGroup") or item_data.get(
				"browseClassification", {}
			).get("displayName")

			if item_group_name:
				item_group = frappe.db.get_value(
					"Item Group", filters={"item_group_name": item_group_name}
				)

				if not item_group:
					new_item_group = frappe.new_doc("Item Group")
					new_item_group.item_group_name = item_group_name
					new_item_group.parent_item_group = (
						self.amz_setting.parent_item_group
					)
					new_item_group.insert()
					return new_item_group.item_group_name
				return item_group

			return self.amz_setting.parent_item_group

		def create_brand(amazon_item) -> str:
			if not amazon_item:
				return

			item_data = get_item_data(amazon_item)
			if not item_data:
				return

			brand_name = item_data.get("Brand") or item_data.get("brand")

			if not brand_name:
				return

			existing_brand = frappe.db.get_value("Brand", filters={"brand": brand_name})

			if not existing_brand:
				brand = frappe.new_doc("Brand")
				brand.brand = brand_name
				brand.insert()
				return brand.brand
			return existing_brand

		def create_manufacturer(amazon_item) -> str:
			if not amazon_item:
				return

			item_data = get_item_data(amazon_item)
			if not item_data:
				return

			manufacturer_name = item_data.get("Manufacturer") or item_data.get(
				"manufacturer"
			)

			if not manufacturer_name:
				return

			existing_manufacturer = frappe.db.get_value(
				"Manufacturer", filters={"short_name": manufacturer_name}
			)

			if not existing_manufacturer:
				manufacturer = frappe.new_doc("Manufacturer")
				manufacturer.short_name = manufacturer_name
				manufacturer.insert()
				return manufacturer.short_name
			return existing_manufacturer

		def create_item_price(amazon_item, item_code) -> None:
			if not amazon_item:
				return

			item_data = get_item_data(amazon_item)
			if not item_data:
				return

			# Check if price already exists for this item and price list
			if frappe.db.exists(
				"Item Price",
				{"item_code": item_code, "price_list": self.amz_setting.price_list},
			):
				return

			item_price = frappe.new_doc("Item Price")
			item_price.price_list = self.amz_setting.price_list
			# Old format has ListPrice.Amount, new format might not have price
			list_price = item_data.get("ListPrice", {})
			if isinstance(list_price, dict):
				item_price.price_list_rate = list_price.get("Amount", 0) or 0
			else:
				item_price.price_list_rate = 0
			item_price.item_code = item_code
			item_price.insert()

		catalog_items = self.get_catalog_items_instance()
		amazon_item_response = catalog_items.get_catalog_item(order_item["ASIN"]) or {}
		amazon_item = amazon_item_response.get("payload", amazon_item_response)

		if not amazon_item:
			return None

		# Check if item already exists
		item_code = order_item["SellerSKU"]
		existing_item_code = frappe.db.exists("Item", item_code)

		if existing_item_code:
			existing_amazon_item_code = frappe.db.get_value(
				"Item", existing_item_code, "amazon_item_code"
			)

			# If amazon_item_code matches, skip without error
			if existing_amazon_item_code == order_item["ASIN"]:
				return existing_item_code

			# If amazon_item_code is different, update the item with new details using db.set_value to avoid locks
			item_group = create_item_group(amazon_item)
			brand = create_brand(amazon_item)

			frappe.db.set_value(
				"Item",
				existing_item_code,
				{
					"item_group": item_group,
					"brand": brand,
					"amazon_item_code": order_item["ASIN"],
					"is_actual_item": 1,
					"is_sales_item": 1,
					"description": order_item["Title"],
				},
				update_modified=False,
			)

			create_item_price(amazon_item, existing_item_code)

			return existing_item_code

		# Create new item
		item = frappe.new_doc("Item")
		item.item_group = create_item_group(amazon_item)
		item.brand = create_brand(amazon_item)
		item.manufacturer = create_manufacturer(amazon_item)
		item.amazon_item_code = order_item["ASIN"]
		item.item_code = order_item["SellerSKU"]
		item.is_actual_item = 1
		item.is_sales_item = 1
		item_name = order_item.get("Title", "") or order_item.get("SellerSKU", "")
		item.item_name = item_name if len(item_name) <= 140 else item_name[:100] + "..."
		item.description = order_item.get('Title', '')

		if default_stock_uom:
			item.stock_uom = default_stock_uom
			item.uom = default_stock_uom
			item.append('uoms', {
				'uom': default_stock_uom,
				'conversion_factor': 1.0,
			})

		#Ignoring Validate and Mandatory to avoid HSN related errors during item creation, HSN can be added later from item master
		item.flags.ignore_mandatory = True
		item.flags.ignore_validate = True
		item.insert(ignore_permissions=True)

		create_item_price(amazon_item, item.item_code)

		return item.name

	def get_item_code(self, order_item, order_id, default_stock_uom=None) -> str:
		item_code = None
		item_asin = order_item.get("ASIN", '')
		result = frappe.db.get_value(
			"Item",
			{"amazon_item_code": item_asin},
			["name", "actual_item"],
			as_dict=True
		)
		if result:
			item_code = result.actual_item or result.name
			return item_code

		if not self.amz_setting.create_item_if_not_exists:
			# Record failed sync attempt
			error_message = f"Failed to create Sales Order against Amazon Order ID : {order_id}. Item with ASIN : {order_item.get('ASIN')} and SKU : {order_item.get('SellerSKU')} not found."
			if not frappe.db.exists("Amazon Failed Sync Record", { "amazon_order_id": order_id, "remarks":error_message }):
				failed_sync_record = frappe.new_doc("Amazon Failed Sync Record")
				failed_sync_record.amazon_order_id = order_id
				failed_sync_record.remarks = error_message
				failed_sync_record.save(ignore_permissions=True)
			return None
		try:
			item_code = self.create_item(order_item, order_id, default_stock_uom=default_stock_uom)
		except Exception as e:
			error_title = f"Item Creation Failed for Amazon Order : {order_id}"
			error_message = f"Error creating item for SKU {order_item['SellerSKU']} and ASIN {order_item['ASIN']}: {str(e)}"
			if not frappe.db.exists('Error Log', {'method': error_title, 'error': error_message}):
				frappe.log_error(message=error_message, title=error_title)
			frappe.msgprint(
				f"Failed to create item for SKU {order_item['SellerSKU']}. Please check error logs for details.",
				alert=True,
				indicator="red",
			)
			# Record failed sync attempt
			if not frappe.db.exists("Amazon Failed Sync Record", {"amazon_order_id": order_id}):
				failed_sync_record = frappe.new_doc("Amazon Failed Sync Record")
				failed_sync_record.amazon_order_id = order_id
				failed_sync_record.remarks = error_message
				failed_sync_record.save(ignore_permissions=True)
		return item_code

	def get_order_items(self, order_id) -> list:
		orders = self.get_orders_instance()
		order_items_payload = self.call_sp_api_method(
			sp_api_method=orders.get_order_items, order_id=order_id
		)
		if not order_items_payload:
			return []

		final_order_items = []
		warehouse = self.amz_setting.warehouse
		default_stock_uom = frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos"

		while True:
			order_items_list = order_items_payload.get("OrderItems")
			next_token = order_items_payload.get("NextToken")

			for order_item in order_items_list:
				zero_qty_flag = False
				actual_qty = 0
				if order_item.get("QuantityOrdered") >= 0:
					item_amount = float(
						order_item.get("ItemPrice", {}).get("Amount", 0)
					)
					item_tax = float(order_item.get("ItemTax", {}).get("Amount", 0))
					# shipping_price = float(order_item.get("ShippingPrice", {}).get("Amount", 0))
					shipping_discount = float(order_item.get("ShippingDiscount", {}).get("Amount", 0))
					total_order_value = item_amount + item_tax
					item_qty = float(order_item.get("QuantityOrdered", 0))
					# In case of Cancelled orders Qty will be 0, Invoice will not get created
					if not item_qty:
						item_qty = 1
						zero_qty_flag = True
						actual_qty = order_item.get("ProductInfo").get("NumberOfItems")
					item_rate = item_amount / item_qty
					item_code = self.get_item_code(order_item, order_id, default_stock_uom=default_stock_uom)
					if not item_code:
						return []

					# Get HSN code from Item master
					item_hsn_code = frappe.db.get_value(
						"Item", item_code, "gst_hsn_code"
					)

					amazon_promotion_discount = float(order_item.get("PromotionDiscount", {}).get("Amount", 0))
					amazon_promotion_discount += shipping_discount

					order_item_dict = {
						"item_code": item_code,
						"item_name": order_item.get("SellerSKU"),
						"description": order_item.get("Title"),
						"rate": item_rate,
						"base_rate": item_rate,
						"qty": item_qty,
						"amount": item_rate * item_qty,
						"base_amount": item_rate * item_qty,
						"uom": default_stock_uom,
						"stock_uom": default_stock_uom,
						"warehouse": warehouse,
						"conversion_factor": 1.0,
						"allow_zero_valuation_rate": 1,
						"total_order_value": total_order_value,
						"zero_qty_flag": zero_qty_flag,
						"actual_qty": actual_qty,
						"amazon_promotion_discount": amazon_promotion_discount,
						"amazon_order_item_id": order_item.get("OrderItemId"),
					}

					# Add HSN code if available
					if item_hsn_code:
						order_item_dict["gst_hsn_code"] = item_hsn_code

					final_order_items.append(order_item_dict)

			if not next_token:
				break

			order_items_payload = self.call_sp_api_method(
				sp_api_method=orders.get_order_items,
				order_id=order_id,
				next_token=next_token,
			)

		return final_order_items

	def create_sales_order(self, order) -> str | None:
		def create_customer(order, customer_name=None) -> str:
			"""
				Create customer based on Amazon order data. If amazon_customer is set in settings, use that. Otherwise, create/find customer based on AmazonOrderId
			"""
			if not customer_name:
				if hasattr(self.amz_setting, 'amazon_customer') and self.amz_setting.amazon_customer:
					if frappe.db.exists("Customer", self.amz_setting.amazon_customer):
						return self.amz_setting.amazon_customer

			order_customer_name = customer_name or order.get("AmazonOrderId", "")

			existing_customer_name = frappe.db.get_value(
				"Customer", filters={"name": order_customer_name}, fieldname="name"
			)

			if existing_customer_name:
				filters = [
					["Dynamic Link", "link_doctype", "=", "Customer"],
					["Dynamic Link", "link_name", "=", existing_customer_name],
					["Dynamic Link", "parenttype", "=", "Contact"],
				]

				existing_contacts = frappe.get_list("Contact", filters)

				if not existing_contacts:
					new_contact = frappe.new_doc("Contact")
					new_contact.first_name = order_customer_name
					new_contact.append(
						"links",
						{
							"link_doctype": "Customer",
							"link_name": existing_customer_name,
						},
					)
					new_contact.insert()

				return existing_customer_name
			else:
				new_customer = frappe.new_doc("Customer")
				new_customer.customer_name = order_customer_name
				new_customer.customer_group = self.amz_setting.customer_group
				new_customer.territory = self.amz_setting.territory
				new_customer.customer_type = self.amz_setting.customer_type
				new_customer.save()

				new_contact = frappe.new_doc("Contact")
				new_contact.first_name = order_customer_name
				new_contact.append(
					"links",
					{"link_doctype": "Customer", "link_name": new_customer.name},
				)

				new_contact.insert()

				return new_customer.name

		def create_address(order, customer_name, map_state_data=0) -> str | None:
			shipping_address = order.get("ShippingAddress")

			if not shipping_address:
				return
			else:
				make_address = frappe.new_doc("Address")
				make_address.address_line1 = shipping_address.get(
					"AddressLine1", "Not Provided"
				)
				make_address.city = shipping_address.get("City", "Not Provided")
				amazon_state = shipping_address.get("StateOrRegion")
				pincode = shipping_address.get("PostalCode")
				make_address.pincode = pincode
				if map_state_data:
					if frappe.db.exists("Amazon State Mapping", {"amazon_state": amazon_state}):
						make_address.state = frappe.db.get_value("Amazon State Mapping",
							{"amazon_state": amazon_state}, "state",
						)
					elif get_state_by_pincode(pincode):
						make_address.state = get_state_by_pincode(pincode)
					else:
						failed_sync_record = frappe.new_doc("Amazon Failed Sync Record")
						failed_sync_record.amazon_order_id = order_id
						failed_sync_record.remarks = (
							"No State Mapping found for {0}".format(amazon_state)
						)
						failed_sync_record.save(ignore_permissions=True)
						return
				else:
					make_address.state = amazon_state

				filters = [
					["Dynamic Link", "link_doctype", "=", "Customer"],
					["Dynamic Link", "link_name", "=", customer_name],
					["Dynamic Link", "parenttype", "=", "Address"],
				]
				existing_address = frappe.get_list("Address", filters)

				for address in existing_address:
					address_doc = frappe.get_doc("Address", address["name"])
					if (
						address_doc.address_line1 == make_address.address_line1
						and address_doc.pincode == make_address.pincode
					):
						return address

				make_address.append(
					"links", {"link_doctype": "Customer", "link_name": customer_name}
				)
				make_address.address_type = "Shipping"
				make_address.is_primary_address = 1
				make_address.is_shipping_address = 1
				make_address.insert()

		def get_refunds(self, order_id, order_date, amazon_order_amount=0, fulfillment_channel=None) -> dict:
			finances = self.get_finances_instance()
			financial_events_payload = self.call_sp_api_method(
				sp_api_method=finances.list_financial_events_by_order_id,
				order_id=order_id,
			)

			if not (
				financial_events_payload
				and financial_events_payload.get("FinancialEvents")
				and financial_events_payload["FinancialEvents"].get("RefundEventList")
			):
				return []

			# Create mapping from SellerSKU to ASIN from order items (for MFN Orders)
			sku_to_asin = {}
			orders = self.get_orders_instance()
			order_items_payload = self.call_sp_api_method(
				sp_api_method=orders.get_order_items, order_id=order_id
			)
			if order_items_payload:
				order_items_list = order_items_payload.get("OrderItems", [])
				for order_item in order_items_list:
					seller_sku = order_item.get("SellerSKU")
					asin = order_item.get("ASIN")
					if seller_sku and asin:
						sku_to_asin[seller_sku] = asin

			refund_events = []

			while True:
				refund_event_list = financial_events_payload.get(
					"FinancialEvents", {}
				).get("RefundEventList", [])
				next_token = financial_events_payload.get("NextToken")

				seller_sku = ""
				for refund_event in refund_event_list:
					if refund_event:
						for refund_item in refund_event.get(
							"ShipmentItemAdjustmentList", []
						):
							charges_and_fees = {
								"posting_date": "",
								"items": [],
								"charges": [],
								"fees": [],
								"tds": [],
								"amazon_order_amount": amazon_order_amount,
								"order_date": order_date,
							}
							charges_and_fees["posting_date"] = format_date_time_to_ist(
								refund_event.get("PostedDate")
							)

							charges = refund_item.get("ItemChargeAdjustmentList", [])
							fees = refund_item.get("ItemFeeAdjustmentList", [])
							promotions = refund_item.get("PromotionAdjustmentList", [])
							seller_sku = refund_item.get("SellerSKU")

							item_code = None
							if fulfillment_channel == "MFN":
								# ASIN Based Item Checking
								asin = refund_item.get("ASIN") or sku_to_asin.get(seller_sku)
								if asin and frappe.db.exists("Item", {"amazon_item_code": asin}):
									item_code = frappe.db.get_value("Item", {"amazon_item_code": asin})
									if frappe.db.get_value("Item", item_code, "actual_item"):
										item_code = frappe.db.get_value("Item", item_code, "actual_item")
							if fulfillment_channel == "AFN":
								# AFN Shipment Log based Item Checking
								item_code = frappe.db.get_value(
									'AFN Order Shipment Log',
									filters={'merchant_sku': seller_sku, 'amazon_order_id': order_id},
									fieldname='item_code'
								)

							for charge in charges:
								charge_type = charge.get("ChargeType")
								amount = charge.get("ChargeAmount", {}).get(
									"CurrencyAmount", 0
								)

								if charge_type != "Principal" and float(amount) != 0:
									charge_account = self.get_account(charge_type)
									charges_and_fees["charges"].append(
										{
											"charge_type": "Actual",
											"account_head": charge_account,
											"tax_amount": amount,
											"description": f"{charge_type} refund for {seller_sku if seller_sku else order_id}",
										}
									)
								else:
									charges_and_fees["items"].append(
										{
											"item_code": item_code,
											"qty": refund_item.get("QuantityShipped"),
											"amount": charge.get("ChargeAmount", {}).get("CurrencyAmount", 0),
											"amazon_order_item_id": refund_item.get("OrderAdjustmentItemId")
										}
									)

							for fee in fees:
								fee_type = fee.get("FeeType")
								amount = fee.get("FeeAmount", {}).get(
									"CurrencyAmount", 0
								)

								if float(amount) != 0:
									fee_account = self.get_account(fee_type)
									charges_and_fees["fees"].append(
										{
											"charge_type": "Actual",
											"account_head": fee_account,
											"tax_amount": amount,
											"description": f"{fee_type} refund for {seller_sku if seller_sku else order_id}",
										}
									)

							for promotion in promotions:
								promotion_type = promotion.get("PromotionType")
								amount = promotion.get("PromotionAmount", {}).get(
									"CurrencyAmount", 0
								)

								if float(amount) != 0:
									promotion_account = self.get_account(promotion_type)
									charges_and_fees["fees"].append(
										{
											"charge_type": "Actual",
											"account_head": promotion_account,
											"tax_amount": amount,
											"description": f"{promotion_type} refund for {seller_sku if seller_sku else order_id}",
										}
									)

							refund_events.append(charges_and_fees)

				if not next_token:
					break

				financial_events_payload = self.call_sp_api_method(
					sp_api_method=finances.list_financial_events_by_order_id,
					order_id=order_id,
					next_token=next_token,
				)

			return refund_events

		order_id = order.get("AmazonOrderId")
		order_date = format_date_time_to_ist(order.get("PurchaseDate"))
		amazon_order_amount = order.get("OrderTotal", {}).get("Amount", 0)
		fulfillment_channel = order.get("FulfillmentChannel")
		company = self.amz_setting.company
		so_id = None
		so_docstatus = 0
		unformated_refunds = get_refunds(self, order_id, order_date, amazon_order_amount, fulfillment_channel)
		refunds = group_returns_by_posting_datetime(unformated_refunds)

		items = self.get_order_items(order_id)
		so_result = frappe.db.get_value(
			"Sales Order",
			{"amazon_order_id": order_id},
			["name", "docstatus"],
			as_dict=True
		)
		so_id = so_result.name if so_result else None
		so_docstatus = so_result.docstatus if so_result else 0

		if so_id and refunds and so_docstatus:
			si = frappe.db.exists(
				"Sales Invoice",
				{"amazon_order_id": order_id, "docstatus": 1, "is_return": 0},
			)
			existing_return_si = frappe.db.exists(
				"Sales Invoice",
				{
					"amazon_order_id": order_id,
					"docstatus": 1,
					"is_return": 1,
					"return_against": si,
				},
			)

			# Preventing returns that are linked in an amazon payment entry from being cancelled
			if existing_return_si and frappe.db.exists(
				"Amazon Payment Entry Item",
				{"order_id": order_id, "return_sales_invoice": existing_return_si},
			):
				return so_id

			if existing_return_si:
				existing_return_si_jv = frappe.db.exists(
					"Journal Entry Account",
					{
						"reference_name": existing_return_si,
						"reference_type": "Sales Invoice",
					},
				)
				if existing_return_si_jv:
					return so_id
				try:
					frappe.get_doc("Sales Invoice", existing_return_si).cancel()
				except Exception as e:
					frappe.log_error(
						title="Error cancelling Return Invoice for {0}".format(existing_return_si),
						message="Exception: {1}".format(existing_return_si, str(e))
					)
					return so_id

			return_created = False
			failed_sync_records_return = { 'payload': [] }
			for refund in refunds:
				return_si = frappe.new_doc("Sales Invoice")
				if not frappe.db.exists(
					"Sales Invoice",
					{"amazon_order_id": order_id, "docstatus": 1, "is_return": 0},
				):
					# Stock Ghosting Process: "Ghosting" refers to adjusting stock for an invoice that lacks sufficient stock, not creating phantom stock.
					if self.amz_setting.adjust_stock_for_returns:
						ghost_stock_si = frappe.db.exists(
							"Sales Invoice",
							{"amazon_order_id": order_id, "is_return": 0},
						)
						if ghost_stock_si:
							ghost_stock_si_doc = frappe.get_doc(
								"Sales Invoice", ghost_stock_si
							)

							if (
								ghost_stock_si_doc.posting_date
								< self.amz_setting.return_invoice_stock_adjustment_before
							):

								# Create Stock Entry
								stock_for_return_created = create_stock_entry(
									ghost_stock_si
								)
								if (
									stock_for_return_created
									and ghost_stock_si_doc.docstatus == 0
								):
									try:
										ghost_stock_si_doc.submit()
									except Exception as e:
										frappe.log_error(
											"Error submitting Invoice {0} for Order ID {1}: {2}".format(
												ghost_stock_si, order_id, str(e)
											)
										)

				sis = frappe.db.get_all("Sales Invoice", {"amazon_order_id": order_id, "docstatus": 1, "is_return": 0}, pluck='name')
				if len(sis)>1:
					if refund.get('items', []):
						refund_amazon_order_item_id = refund.get('items', [])[0].get('amazon_order_item_id', '')
						if refund_amazon_order_item_id:
							si = get_sales_invoice_id_from_item_id(order_id, refund_amazon_order_item_id)
							if get_so_id_from_si(si):
								so_id = get_so_id_from_si(si)

				# If not found, check for draft invoice
				if not si:
					si = frappe.db.get_value(
						"Sales Invoice",
						{"amazon_order_id": order_id, "docstatus": 0, "is_return": 0},
					)
					if si:
						# Try to submit the draft invoice
						try:
							si_doc = frappe.get_doc("Sales Invoice", si)
							si_doc.submit()
						except Exception as e:
							frappe.db.rollback()
							frappe.log_error(
								title="Return Invoice - Failed to Submit Draft Invoice",
								message=f"Order ID: {order_id}, Invoice: {si}, Error: {str(e)}",
							)
							return

				if not si:
					# Check if failed sync record already exists for this order_id with similar return invoice error
					remarks = f"Failed to create return Sales Invoice, No Sales Invoice found for Amazon Order ID: {order_id}. Sales Order ID: {so_id}"
					existing_records = frappe.get_all(
						"Amazon Failed Sync Record",
						filters={
							"amazon_order_id": order_id,
							"remarks": remarks,
						},
						limit=1,
					)
					if not existing_records:
						failed_sync_records_return['amazon_order_id'] = order_id
						failed_sync_records_return['remarks'] = remarks
						failed_sync_records_return['is_return_exception'] = 1
						if refund.get("posting_date"):
							failed_sync_records_return['posting_date'] = dateutil.parser.parse(refund.get("posting_date")).strftime("%Y-%m-%d")
						if refund.get("order_date"):
							failed_sync_records_return['amazon_order_date'] = dateutil.parser.parse(refund.get("order_date")).strftime("%Y-%m-%d")
						if refund.get("amazon_order_amount"):
							failed_sync_records_return['amazon_order_amount'] = refund.get("amazon_order_amount")
						existing_payload = failed_sync_records_return.get('payload', [])
						existing_payload.append(refund)
						failed_sync_records_return['payload'] = existing_payload
					continue

				existing_returns = tuple(
					frappe.db.get_all(
						"Sales Invoice", {"return_against": si}, pluck="name"
					)
				)

				# Set return invoice basic properties once per refund
				try:
					if refund.get("posting_date"):
						posting_date = refund.get("posting_date")
						return_si.posting_date = getdate(posting_date)
						return_si.posting_time = get_datetime(posting_date).strftime(
							"%H:%M:%S"
						)
						return_si.set_posting_time = 1
				except Exception as e:
					frappe.log_error(message=f"Error setting posting date for return invoice: {str(e)}", title="Return Invoice - Posting Date Error")

				return_si.is_return = 1
				return_si.update_stock = 1
				return_si.return_against = si
				si_data = frappe.db.get_value(
					"Sales Invoice", si,
					["customer", "company", "debit_to", "set_warehouse", "fulfillment_channel", "status"],
					as_dict=True
				)

				return_si.customer = si_data.customer
				return_si.company  = si_data.company
				return_si.debit_to = si_data.debit_to
				return_warehouse   = si_data.set_warehouse
				if self.amz_setting.temporary_stock_transfer_required:
					return_warehouse = self.amz_setting.warehouse
					si_fulfilement_channel = si_data.fulfillment_channel
					if si_fulfilement_channel:
						if si_fulfilement_channel == "AFN":
							return_warehouse = self.amz_setting.afn_warehouse
				return_si.set_warehouse = return_warehouse
				return_si.amazon_order_id = order_id
				# Set status from original sales invoice
				si_status = si_data.status
				if si_status:
					return_si.status = si_status

				# Process items for this refund
				refund_items_processed = False
				returned_qty_map = {}
				if existing_returns:
					returned_qty_rows = frappe.db.get_all(
						"Sales Invoice Item",
						filters={"parent": ["in", existing_returns]},
						fields=["item_code", "qty"]
					)
					for row in returned_qty_rows:
						returned_qty_map[row.item_code] = returned_qty_map.get(row.item_code, 0) + (row.qty or 0)

				si_items_data = frappe.db.get_all(
					"Sales Invoice Item",
					filters={"parent": si},
					fields=["name", "item_code", "qty", "rate", "gst_hsn_code", "warehouse"]
				)
				si_item_map = {row.item_code: row for row in si_items_data}

				for item in refund.get("items", []):
					if not item.get("item_code"):
						continue

					actual_item = frappe.db.get_value("Item", item.get("item_code"), "actual_item")
					if not actual_item:
						actual_item = item.get("item_code")

					if not actual_item:
						continue

					returned_qty = returned_qty_map.get(actual_item, 0)
					item_qty = float(item.get("qty") or 0)
					amazon_order_item_id = item.get("amazon_order_item_id", '')
					item_amount = float(item.get("amount") or 0)
					si_item = si_item_map.get(actual_item)
					if not si_item:
						continue
					original_qty = si_item.qty or 0
					if original_qty >= (returned_qty + item_qty):
						rate = abs(item_amount / item_qty) if item_qty else (si_item.rate or 0)
						original_item_hsn = si_item.gst_hsn_code or frappe.db.get_value("Item", actual_item, "gst_hsn_code")

						return_item = {
							"item_code": actual_item,
							"qty": -1 * item_qty,
							"rate": rate,
							"sales_order": so_id,
							"sales_invoice_item": si_item.name,
							"amazon_order_item_id": amazon_order_item_id,
							"warehouse": si_item.warehouse
						}
						if original_item_hsn:
							return_item["gst_hsn_code"] = original_item_hsn

						return_si.append("items", return_item)
						frappe.db.set_value(
							"Sales Invoice Item",
							{"parent": si, "item_code": actual_item},
							"refunded",
							1,
						)
						refund_items_processed = True
						return_created = True

				# Add charges and fees if items were processed
				if refund_items_processed or len(return_si.get('items', []))>0:
					for charge in refund.get("charges", []):
						return_si.append("taxes", charge)

					for fee in refund.get("fees", []):
						return_si.append("taxes", fee)

					for tds in refund.get("tds", []):
						return_si.append("taxes", tds)

					return_si.disable_rounded_total = 1
					return_si.update_outstanding_for_self = 1
					return_si.update_billed_amount_in_sales_order = 1

				# Only insert and submit if items were created
				if return_created and len(return_si.items) > 0:
					company_abr, default_cc = frappe.db.get_value('Company', return_si.company, ['abbr', 'cost_center'])
					for row in return_si.taxes:
						if row.cost_center:
							current_cc = row.cost_center
							new_cc = re.sub(r"-[^-]*$", f"- {company_abr}", current_cc)
							row.cost_center = new_cc if frappe.db.exists('Cost Center', new_cc) else default_cc
						if row.account_head:
							row.account_head = get_account_head(row.account_head, return_si.company)
					try:
						return_si.insert(ignore_permissions=True)
						return_si.submit()
					except Exception as e:
						error_msg = str(e)
						# Enhance HSN/SAC errors with item information
						enhanced_error = self.enhance_hsn_error_with_items(
							error_msg, return_si
						)
						frappe.log_error(
							title="Error creating Return Invoice",
							message=f"Order ID: {order_id or 'None'}, Error: {enhanced_error}",
						)

			#Process Return Invoice Data for failed sync records
			if failed_sync_records_return.get('remarks') and failed_sync_records_return.get('payload'):
				if not frappe.db.exists("Amazon Failed Sync Record", {
					"amazon_order_id": order_id,
					"remarks": failed_sync_records_return.get('remarks'),
					"is_return_exception": 1
				}):
					failed_sync_record = frappe.new_doc("Amazon Failed Sync Record")
					failed_sync_record.amazon_order_id = order_id
					failed_sync_record.remarks = failed_sync_records_return.get('remarks')
					failed_sync_record.is_return_exception = 1
					if failed_sync_records_return.get('posting_date'):
						failed_sync_record.posting_date = failed_sync_records_return.get('posting_date')
					if failed_sync_records_return.get('amazon_order_date'):
						failed_sync_record.amazon_order_date = failed_sync_records_return.get('amazon_order_date')
					if failed_sync_records_return.get('amazon_order_amount'):
						failed_sync_record.amazon_order_amount = failed_sync_records_return.get('amazon_order_amount')
					if failed_sync_records_return.get('payload'):
						failed_sync_record.payload = frappe.as_json(failed_sync_records_return.get('payload'))
					failed_sync_record.save(ignore_permissions=True)

			return so_id

		else:
			if so_docstatus and so_id:
				return so_id
			if not so_id:
				so = frappe.new_doc("Sales Order")
			else:
				so = frappe.get_doc("Sales Order", so_id)

			delivery_date = format_date_time_to_ist(order.get("LatestShipDate"))
			transaction_date = format_date_time_to_ist(order.get("PurchaseDate"))

			so.amazon_order_id = order_id
			so.marketplace_id = order.get("MarketplaceId")
			so.amazon_order_status = order.get("OrderStatus")
			so.fulfillment_channel = order.get("FulfillmentChannel")
			so.replaced_order_id = order.get("ReplacedOrderId") or ""
			customer_name = ''
			if order.get("IsBusinessOrder"):
				so.amazon_customer_type = "B2B"
				buyer_info = self.call_sp_api_method(
					sp_api_method=self.get_orders_instance().get_buyer_info,
					order_id=order_id
				)
				customer_name = (buyer_info or {}).get('BuyerTaxInfo', {}).get('CompanyLegalName', '')
			else:
				so.amazon_customer_type = "B2C"

			customer = create_customer(order, customer_name)

			if order.get("IsBusinessOrder"):
				create_address(order, customer, self.amz_setting.map_state_data)
			if amazon_order_amount:
				so.amazon_order_amount = amazon_order_amount
			so.amazon_order_status = order.get("OrderStatus")
			so.customer = customer
			so.delivery_date = (
				delivery_date
				if getdate(delivery_date) > getdate(transaction_date)
				else transaction_date
			)
			so.transaction_date = get_datetime(transaction_date).strftime("%Y-%m-%d")
			so.transaction_time = get_datetime(transaction_date).strftime("%H:%M:%S")
			warehouse = self.amz_setting.warehouse
			if so.fulfillment_channel:
				if so.fulfillment_channel == "AFN":
					warehouse = self.amz_setting.afn_warehouse
			if self.amz_setting.temporary_stock_transfer_required:
				warehouse = self.amz_setting.temporary_order_warehouse

			if so.fulfillment_channel == "AFN" and self.amz_setting.fc_based_invoice_creation:
				if so.company:
					company = so.company
				if so.set_warehouse:
					warehouse = so.set_warehouse
			so.company = company
			so.set_warehouse = warehouse

			items = self.get_order_items(order_id)

			if not items:
				if not so_id:
					return
				else:
					so.flags.ignore_mandatory = True
					so.disable_rounded_total = 1
					so.custom_validate()
					if so.grand_total >= 0:
						so.save(ignore_permissions=True)
					elif not frappe.db.exists(
						"Amazon Failed Sync Record", {"amazon_order_id": order_id}
					):
						remarks = "Failed to create Sales Order for {0}. Sales Order grand Total = {1}".format(
							order_id, so.grand_total
						)
						failed_sync_record = frappe.new_doc("Amazon Failed Sync Record")
						failed_sync_record.amazon_order_id = order_id
						failed_sync_record.remarks = remarks
						failed_sync_record.payload = so.as_dict()
						failed_sync_record.replaced_order_id = so.replaced_order_id
						failed_sync_record.posting_date = so.transaction_date
						failed_sync_record.amazon_order_date = so.transaction_date
						failed_sync_record.grand_total = so.grand_total
						failed_sync_record.amazon_order_amount = so.amazon_order_amount
						if not frappe.db.exists(
							"Amazon Failed Sync Record",
							{
								"amazon_order_id": order_id,
								"remarks": remarks,
								"grand_total": so.grand_total,
							},
						):
							failed_sync_record.save(ignore_permissions=True)
					return

			so.items = []
			so.taxes = []
			so.taxes_and_charges = ""
			total_order_value = 0

			# Check if all items are zero-qty
			all_zero_qty = all(item.get("zero_qty_flag", False) for item in items)
			zero_qty_items = []

			for item in items:
				if not all_zero_qty and item.get("zero_qty_flag", True):
					zero_qty_items.append(item)
					continue

				total_order_value += item.get("total_order_value", 0)
				item["warehouse"] = warehouse

				# Ensure HSN code is set from Item master if not already present
				if not item.get("gst_hsn_code") and item.get("item_code"):
					item_hsn_code = frappe.db.get_value("Item", item.get("item_code"), "gst_hsn_code")
					if item_hsn_code:
						item["gst_hsn_code"] = item_hsn_code

				so.append("items", item)

			if len(zero_qty_items) > 0:
				so.cancelled_items = []
				for zero_item in zero_qty_items:
					so.append(
						"cancelled_items",
						{
							"cancelled_item_code": zero_item.get("item_code"),
							"cancelled_item_qty": zero_item.get("actual_qty"),
						},
					)

			if total_order_value:
				so.amazon_order_amount = total_order_value

			taxes_and_charges = self.amz_setting.taxes_charges

			item_lookup = {
				item["item_code"]: item.get("total_order_value", 0) for item in items
			}
			for row in so.items:
				total_value = item_lookup.get(row.item_code)
				if total_value:
					row.total_order_value = total_value

			if taxes_and_charges:
				charges_and_fees = self.get_charges_and_fees(order_id, company)
				if charges_and_fees.get("principal_amounts"):
					principal_amounts = charges_and_fees.get("principal_amounts")
					for item_row in so.items:
						if item_row.item_name and principal_amounts.get(
							item_row.item_name
						):
							pricipal_amount = (
								float(principal_amounts.get(item_row.item_name)) or 0
							)
							qty = item_row.qty
							if pricipal_amount:
								item_row.rate = pricipal_amount
								item_row.base_rate = pricipal_amount
								item_row.amount = pricipal_amount * qty
								item_row.base_amount = pricipal_amount * qty

				for charge in charges_and_fees.get("charges"):
					if charge:
						so.append("taxes", charge)

				for fee in charges_and_fees.get("fees"):
					if fee:
						so.append("taxes", fee)

				for tds in charges_and_fees.get("tds"):
					if tds:
						so.append("taxes", tds)

				if not refunds:
					journal_entry_series = frappe.db.get_single_value("eSeller Settings", "journal_entry_series")
					for service_fee in charges_and_fees.get("service_fees"):
						if service_fee:
							mfn_postage_fee_account_head = frappe.db.get_value(
								"Amazon SP API Settings", self.amz_setting.name, "mfn_postage_fee_account_head",
							)
							if (
								not service_fee.get("account_head")
								== mfn_postage_fee_account_head
							) or so.replaced_order_id:
								so.append("taxes", service_fee)
							elif not frappe.db.exists(
								"Journal Entry Account",
								{
									"amazon_order_id": so.amazon_order_id,
									"account": service_fee.get("account_head"),
									"debit_in_account_currency": abs(service_fee.get("tax_amount")),
								},
							):
								try:
									if not journal_entry_series:
										frappe.throw(_("Please set Journal Entry Series in eSeller Settings"))
									jv_doc = frappe.new_doc("Journal Entry")
									jv_doc.voucher_type = "Journal Entry"
									jv_doc.naming_series = journal_entry_series
									jv_doc.posting_date = so.transaction_date
									jv_doc.user_remark = f"Amazon MFN Postage Fee for Order {so.amazon_order_id}"
									jv_doc.amazon_order_id = so.amazon_order_id
									tax_amount = abs(
										float(service_fee.get("tax_amount", 0))
									)
									jv_row = jv_doc.append("accounts")
									jv_row.account = service_fee.get("account_head")
									jv_row.debit = tax_amount
									jv_row.debit_in_account_currency = tax_amount
									jv_row.user_remark = row.get("description")
									jv_row.amazon_order_id = so.amazon_order_id
									default_receivable_account = frappe.db.get_value("Company", self.amz_setting.company, "default_receivable_account")
									jv_row = jv_doc.append("accounts")
									jv_row.credit = abs(float(service_fee.get("tax_amount", 0)))
									jv_row.credit_in_account_currency = abs(float(service_fee.get("tax_amount", 0)))
									jv_row.user_remark = f"Amazon MFN Postage Fee for Order {so.amazon_order_id}"
									jv_row.amazon_order_id = so.amazon_order_id
									jv_row.party_type = "Customer"
									jv_row.party = so.get("customer")
									jv_row.account = default_receivable_account
									jv_doc.flags.ignore_mandatory = True
									jv_doc.save(ignore_permissions=True)
									jv_doc.submit()
								except Exception as e:
									frappe.log_error(
										title="Error creating Journal Entry for MFN Postage Fee",
										message=f"Order ID: {so.amazon_order_id}, Error: {str(e)}",
									)

				if charges_and_fees.get("additional_discount"):
					so.discount_amount = float(charges_and_fees.get("additional_discount")) * -1

			so.flags.ignore_mandatory = True
			so.disable_rounded_total = 1
			so.custom_validate()
			if so.grand_total >= 0:
				try:
					so.save(ignore_permissions=True)
				except Exception as e:
					error_msg = str(e)
					# Enhance HSN/SAC errors with item information
					enhanced_error = self.enhance_hsn_error_with_items(error_msg, so)
					frappe.log_error(
						title="Error saving Sales Order for Order {0}".format(so.amazon_order_id),
						message=f"Error: {enhanced_error}",
					)

				order_statuses = [
					"Shipped",
					"InvoiceUnconfirmed",
					"Unfulfillable",
				]

				order_status_valid = order.get("OrderStatus") in order_statuses
				has_taxes = len(so.taxes) > 0
				temp_transfer_required = (
					self.amz_setting.temporary_stock_transfer_required
				)

				transfer_exists = (
					frappe.db.exists(
						"Stock Entry",
						{"name": so.temporary_stock_tranfer_id, "docstatus": 1},
					)
					if temp_transfer_required
					else True
				)

				fc_data_exists = True
				if so.fulfillment_channel == "AFN" and self.amz_setting.fc_based_invoice_creation:
					fc_data_exists = True if so.fc_location else False

				if order_status_valid and has_taxes and transfer_exists and fc_data_exists:
					try:
						so.submit()
					except Exception as e:
						error_msg = str(e)
						# Enhance HSN/SAC errors with item information
						enhanced_error = self.enhance_hsn_error_with_items(
							error_msg, so
						)
						frappe.log_error(
							title="Error submitting Sales Order for Order {0}".format(
								so.amazon_order_id
							),
							message=f"Error: {enhanced_error}",
						)
			elif not frappe.db.exists("Amazon Failed Sync Record", {"amazon_order_id": order_id}):
				remarks = "Failed to create Sales Order for {0}. Sales Order grand Total = {1}".format(
					order_id, so.grand_total
				)
				failed_sync_record = frappe.new_doc("Amazon Failed Sync Record")
				failed_sync_record.amazon_order_id = order_id
				failed_sync_record.remarks = remarks
				failed_sync_record.replaced_order_id = so.replaced_order_id
				failed_sync_record.posting_date = so.transaction_date
				failed_sync_record.amazon_order_date = so.transaction_date
				failed_sync_record.grand_total = so.grand_total
				failed_sync_record.amazon_order_amount = so.amazon_order_amount
				if not so_id:
					failed_sync_record.payload = so.as_dict()
				if not frappe.db.exists(
					"Amazon Failed Sync Record",
					{
						"amazon_order_id": order_id,
						"grand_total": so.grand_total,
						"remarks": remarks,
					},
				):
					failed_sync_record.save(ignore_permissions=True)

			return so.name

	def get_orders(self, last_updated_after, sync_selected_date_only=0, amazon_order_ids=None) -> list:
		orders = self.get_orders_instance()

		order_statuses = [
			"Shipped",
			"InvoiceUnconfirmed",
			"Canceled",
			"Unfulfillable",
		]
		fulfillment_channels = ["AFN", "MFN"]
		orders_payload = None
		amazon_order_ids = amazon_order_ids if amazon_order_ids else ''
		try:
			if sync_selected_date_only:
				last_updated_before = add_days(getdate(last_updated_after), 1).strftime(
					"%Y-%m-%d"
				)
				orders_payload = self.call_sp_api_method(
					sp_api_method=orders.get_orders,
					last_updated_after=last_updated_after,
					last_updated_before=last_updated_before,
					order_statuses=order_statuses,
					fulfillment_channels=fulfillment_channels,
					amazon_order_ids = amazon_order_ids,
					max_results=50,
				)
			else:
				orders_payload = self.call_sp_api_method(
					sp_api_method=orders.get_orders,
					last_updated_after=to_iso_z(last_updated_after),
					order_statuses=order_statuses,
					fulfillment_channels=fulfillment_channels,
					amazon_order_ids = amazon_order_ids,
					max_results=50,
				)
		except Exception as e:
			frappe.log_error(title="GET Orders", message=frappe.get_traceback(e))

		sales_orders = []
		while True:
			if orders_payload:
				orders_list = orders_payload.get("Orders")
				next_token = orders_payload.get("NextToken")
				if not orders_list or len(orders_list) == 0:
					break
				for order in orders_list:
					order_id = order.get("AmazonOrderId", "Unknown")
					try:
						sales_order = self.create_sales_order(order)
						if sales_order:
							sales_orders.append(sales_order)
					except Exception as e:
						error_msg = str(e)
						# Enhance HSN/SAC errors with item information
						enhanced_error = error_msg
						if "HSN/SAC" in error_msg or "hsn_code" in error_msg.lower():
							# Try to get the sales order to extract item information
							try:
								so_name = frappe.db.get_value(
									"Sales Order", {"amazon_order_id": order_id}, "name"
								)
								if so_name:
									so_doc = frappe.get_doc("Sales Order", so_name)
									enhanced_error = self.enhance_hsn_error_with_items(
										error_msg, so_doc
									)
							except Exception as e:
								pass

						# Log error and skip this order, continue with next order
						frappe.log_error(
							title=f"Failed to sync Amazon Order: {order_id}",
							message=f"Order ID: {order_id}\nError: {enhanced_error}",
						)
						# Create failed sync record if it doesn't exist
						if not frappe.db.exists(
							"Amazon Failed Sync Record", {"amazon_order_id": order_id}
						):
							try:
								failed_sync_record = frappe.new_doc(
									"Amazon Failed Sync Record"
								)
								failed_sync_record.amazon_order_id = order_id
								failed_sync_record.remarks = (
									f"Failed to sync order: {enhanced_error}"
								)
								if order.get("PurchaseDate"):
									order_date = format_date_time_to_ist(
										order.get("PurchaseDate")
									)
									failed_sync_record.amazon_order_date = getdate(
										order_date
									).strftime("%Y-%m-%d")
								failed_sync_record.save(ignore_permissions=True)
							except Exception as save_error:
								frappe.log_error(
									title=f"Failed to create Amazon Failed Sync Record for {order_id}",
									message=str(save_error),
								)
						continue
				if not next_token:
					break
				orders_payload = self.call_sp_api_method(
					sp_api_method=orders.get_orders,
					last_updated_after=last_updated_after,
					next_token=next_token,
				)
			else:
				break
		frappe.enqueue("eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings.enq_si_submit", queue="long")
		return sales_orders

	def get_order(self, amazon_order_ids, run_si_submit_job=1) -> list:
		orders = self.get_orders_instance()
		order_payload = self.call_sp_api_method(
			sp_api_method=orders.get_order,
			order_id=amazon_order_ids,
		)
		# order_call = orders.get_order(order_id=amazon_order_ids)
		sales_orders = []
		if order_payload:
			order_id = order_payload.get("AmazonOrderId", amazon_order_ids)
			try:
				sales_order = self.create_sales_order(order_payload)
				if sales_order:
					sales_orders.append(sales_order)
			except Exception as e:
				error_msg = str(e)
				# Enhance HSN/SAC errors with item information
				enhanced_error = error_msg
				if "HSN/SAC" in error_msg or "hsn_code" in error_msg.lower():
					# Try to get the sales order to extract item information
					try:
						so_name = frappe.db.get_value(
							"Sales Order", {"amazon_order_id": order_id}, "name"
						)
						if so_name:
							so_doc = frappe.get_doc("Sales Order", so_name)
							enhanced_error = self.enhance_hsn_error_with_items(
								error_msg, so_doc
							)
					except Exception as e:
						pass

				# Log error and skip this order
				frappe.log_error(
					title=f"Failed to sync Amazon Order: {order_id}",
					message=f"Order ID: {order_id}\nError: {enhanced_error}",
				)
				# Create failed sync record if it doesn't exist
				if not frappe.db.exists(
					"Amazon Failed Sync Record", {"amazon_order_id": order_id}
				):
					try:
						failed_sync_record = frappe.new_doc("Amazon Failed Sync Record")
						failed_sync_record.amazon_order_id = order_id
						failed_sync_record.remarks = enhanced_error
						if order_payload.get("PurchaseDate"):
							order_date = format_date_time_to_ist(
								order_payload.get("PurchaseDate")
							)
							failed_sync_record.amazon_order_date = getdate(
								order_date
							).strftime("%Y-%m-%d")
						failed_sync_record.save(ignore_permissions=True)
					except Exception as save_error:
						frappe.log_error(
							title=f"Failed to create Amazon Failed Sync Record for {order_id}",
							message=str(save_error),
						)
				# Re-throw with enhanced error message
				frappe.throw(enhanced_error)
		if run_si_submit_job == 1:
			frappe.enqueue("eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings.enq_si_submit", queue="long")
		return sales_orders

	def get_catalog_items_instance(self) -> CatalogItems:
		return CatalogItems(**self.instance_params)

def to_iso_z(d):
	# Accept either date (YYYY-MM-DD) or datetime
	if isinstance(d, str):
		# If it's already an ISO string with time, assume caller passed correct value
		if "T" in d:
			return d
		# assume YYYY-MM-DD
		d = datetime.strptime(d, "%Y-%m-%d").date()
	if isinstance(d, date) and not isinstance(d, datetime):
		# midnight UTC
		dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
	else:
		dt = d.astimezone(timezone.utc)
	return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

@frappe.whitelist()
def get_orders(amz_setting_name, last_updated_after, sync_selected_date_only=0, amazon_order_ids=None) -> list:
	ar = AmazonRepository(amz_setting_name)
	return ar.get_orders(last_updated_after, sync_selected_date_only, amazon_order_ids)

@frappe.whitelist()
def get_order(amz_setting_name, amazon_order_ids, run_si_submit_job=1) -> list:
	ar = AmazonRepository(amz_setting_name)
	return ar.get_order(amazon_order_ids, run_si_submit_job)

@frappe.whitelist()
def get_order_shipment_charges(amz_setting_name, amazon_order_ids) -> list:
	ar = AmazonRepository(amz_setting_name)
	return ar.get_charges_and_fees(amazon_order_ids)

@frappe.whitelist()
def create_stock_entry(sales_invoice):
	"""
	Method to create Stock entry for Returns and Replaced Orders
	"""
	stock_entry_created = False
	if frappe.db.exists("Sales Invoice", sales_invoice):
		si_doc = frappe.get_doc("Sales Invoice", sales_invoice)
		stock_entry = frappe.new_doc("Stock Entry")
		stock_entry.update(
			{
				"stock_entry_type": "Material Receipt",
				"set_posting_time": 1,
				"posting_date": si_doc.posting_date,
				"posting_time": si_doc.posting_time,
				"sales_invoice_no": si_doc.name,
				"from_return_invoice": 1,
				"remarks": "Stock updated to reflect return/replacement for Amazon Order {0}".format(
					si_doc.amazon_order_id
				),
				"to_warehouse": si_doc.set_warehouse,
			}
		)

		# Setting Items
		for item in si_doc.items:
			stock_entry.append(
				"items",
				{
					"item_code": item.item_code,
					"qty": item.qty,
					"allow_zero_valuation_rate": 1,
				},
			)

		# Savepoint for rollback safety
		frappe.db.savepoint("ghost_stocking")
		try:
			stock_entry.insert(ignore_permissions=True)
			stock_entry.submit()
			stock_entry_created = True
		except Exception as e:
			stock_entry_created = False
			frappe.db.rollback(save_point="ghost_stocking")
			frappe.get_doc(
				{
					"doctype": "Amazon Failed Invoice Record",
					"invoice_id": sales_invoice,
					"error": str(e),
				}
			).insert()
	return stock_entry_created


@frappe.whitelist()
def create_stock_entries_for_sis(names):
	"""method to bulk create stock entries for draft sales invoices"""
	names = json.loads(names)
	for name in names:
		create_stock_entry(name)

def generate_random_string(length=4):
	'''Generates a random string of specified length consisting of letters and digits.'''
	import random
	import string

	characters = string.ascii_letters + string.digits
	random_string = ''.join(random.choices(characters, k=length))

	return random_string

def get_sales_invoice_id_from_item_id(order_id, amazon_order_item_id):
	'''
		Method to get Sales Inovice ID using order_id and order_item_id
	'''
	result = frappe.db.sql("""
		SELECT
			si.name
		FROM
			`tabSales Invoice` si
		INNER JOIN
			`tabSales Invoice Item` sii
			ON sii.parent = si.name
		WHERE
			si.amazon_order_id = %s
			AND sii.amazon_order_item_id = %s
			AND si.docstatus = 1
		LIMIT 1
	""", (order_id, amazon_order_item_id))

	return result[0][0] if result else None

def get_so_id_from_si(sales_invoice):
	'''
		Method to get Sales Order ID from Sales Invoice Reference
	'''
	result = frappe.db.sql("""
		SELECT DISTINCT
			sii.sales_order
		FROM
			`tabSales Invoice Item` sii
		WHERE
			sii.parent = %s
			AND sii.sales_order IS NOT NULL
		LIMIT 1
	""", (sales_invoice))

	return result[0][0] if result else None

def group_returns_by_posting_datetime(payload):
	grouped_returns = []
	grouped_by_posting_date = {}

	for refund in payload:
		items = refund.get("items") or []
		charges = refund.get("charges") or []
		fees = refund.get("fees") or []
		tds = refund.get("tds") or []
		group = {}

		if items:
			posting_date = refund.get("posting_date")

			if posting_date not in grouped_by_posting_date:
				group = {
					"amazon_order_amount": refund.get("amazon_order_amount"),
					"charges": [],
					"fees": [],
					"items": [],
					"tds": [],
					"order_date": refund.get("order_date"),
					"posting_date": posting_date
				}

				grouped_by_posting_date[posting_date] = group
				grouped_returns.append(group)

			else:
				group = grouped_by_posting_date[posting_date]

			group["items"].extend(items)
			group["charges"].extend(charges)
			group["fees"].extend(fees)
			group["tds"].extend(tds)

	return grouped_returns

import urllib.request
#Only these values have change with respect to api data
state_mapping = {
	'Andaman & Nicobar': 'Andaman and Nicobar Islands',
	'Dadra & Nagar Haveli': 'Dadra and Nagar Haveli and Daman and Diu',
	'Daman & Diu': 'Dadra and Nagar Haveli and Daman and Diu',
	'Jammu & Kashmir': 'Jammu and Kashmir',
	'Lakshadweep': 'Lakshadweep Islands',
	'Pondicherry': 'Puducherry'
}
def get_state_by_pincode(pincode):
	"""
	Get state from Indian PIN code.
	"""
	try:
		url = f"https://api.postalpincode.in/pincode/{pincode}"

		with urllib.request.urlopen(url, timeout=10) as response:
			data = json.loads(response.read().decode())

		if (
			data
			and data[0].get("Status") == "Success"
			and data[0].get("PostOffice")
		):
			state = data[0]["PostOffice"][0]["State"]
			if state_mapping.get(state):
				state = state_mapping.get(state)
			return state

	except Exception:
		frappe.log_error(message=frappe.get_traceback(), title="Get State by Pincode")

	return None