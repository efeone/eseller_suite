# Copyright (c) 2024, efeone and contributors
# For license information, please see license.txt


import json
import time
import urllib

import dateutil
import frappe
from frappe import _
from datetime import datetime
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

	def call_sp_api_method(self, sp_api_method, **kwargs) -> dict:
		errors = {}
		max_retries = self.amz_setting.max_retry_limit

		for x in range(max_retries):
			try:
				result = sp_api_method(**kwargs)
				return result.get("payload")
			except SPAPIError as e:
				if e.error not in errors:
					errors[e.error] = e.error_description

				time.sleep(1)
				continue

		for error in errors:
			msg = f"<b>Error:</b> {error}<br/><b>Error Description:</b> {errors.get(error)}"
			frappe.msgprint(msg, alert=True, indicator="red")
			frappe.log_error(
				message=f"{error}: {errors.get(error)}", title=f'Method "{sp_api_method.__name__}" failed',
			)

		self.amz_setting.enable_sync = 0
		self.amz_setting.save()

		frappe.throw(
			_("Scheduled sync has been temporarily disabled because maximum retries have been exceeded!")
		)

	def get_finances_instance(self) -> Finances:
		return Finances(**self.instance_params)

	def get_account(self, name) -> str:
		account_name = frappe.db.get_value("Account", {"account_name": "Amazon {0}".format(name)})

		if not account_name:
			new_account = frappe.new_doc("Account")
			new_account.account_name = "Amazon {0}".format(name)
			new_account.company = self.amz_setting.company
			new_account.parent_account = self.amz_setting.market_place_account_group
			new_account.insert(ignore_permissions=True)
			account_name = new_account.name

		return account_name

	def get_charges_and_fees(self, order_id) -> dict:
		finances = self.get_finances_instance()
		financial_events_payload = self.call_sp_api_method(
			sp_api_method=finances.list_financial_events_by_order_id, order_id=order_id
		)

		charges_and_fees = {"charges": [], "fees": [], "tds": [], "service_fees":[], "principal_amounts":{}, "additional_discount": 0}
  
		if not (
				financial_events_payload
				and len(financial_events_payload.get("FinancialEvents", {}))
			):
				return charges_and_fees

		while True:
			shipment_event_list = financial_events_payload.get("FinancialEvents", {}).get(
				"ShipmentEventList", []
			)
			service_fee_event_list = financial_events_payload.get("FinancialEvents", {}).get(
				"ServiceFeeEventList", []
			)
			next_token = financial_events_payload.get("NextToken")
			principal_amounts = {}
			promotion_discount = 0
			seller_sku = ''
			for shipment_event in shipment_event_list:
				if shipment_event:
					for shipment_item in shipment_event.get("ShipmentItemList", []):
						promotion_list = shipment_item.get("PromotionList", [])
						seller_sku = shipment_item.get("SellerSKU")
						qty = shipment_item.get("QuantityShipped")
						charges = shipment_item.get("ItemChargeList", [])
						fees = shipment_item.get("ItemFeeList", [])
						tds_list = shipment_item.get("ItemTaxWithheldList", [])
						tdss = []
						if tds_list:
							tdss = tds_list[0].get("TaxesWithheld", [])

						for charge in charges:
							charge_type = charge.get("ChargeType")
							amount = charge.get("ChargeAmount", {}).get("CurrencyAmount", 0)

							if charge_type != "Principal" and float(amount) != 0:
								charge_account = self.get_account(charge_type)
								charges_and_fees.get("charges").append(
									{
										"charge_type": "Actual",
										"account_head": charge_account,
										"tax_amount": amount,
										"description": charge_type + " for " + seller_sku,
									}
								)
							if charge_type == 'Principal':
								principal_amounts[seller_sku] = round((float(amount)/qty), 2)

						for fee in fees:
							fee_type = fee.get("FeeType")
							amount = fee.get("FeeAmount", {}).get("CurrencyAmount", 0)

							if float(amount) != 0:
								fee_account = self.get_account(fee_type)
								charges_and_fees.get("fees").append(
									{
										"charge_type": "Actual",
										"account_head": fee_account,
										"tax_amount": amount,
										"description": fee_type + " for " + seller_sku,
									}
								)

						for tds in tdss:
							tds_type = tds.get("ChargeType")
							amount = tds.get("ChargeAmount", {}).get("CurrencyAmount", 0)
							if float(amount) != 0:
								tds_account = self.get_account(tds_type)
								charges_and_fees.get("tds").append(
									{
										"charge_type": "Actual",
										"account_head": tds_account,
										"tax_amount": amount,
										"description": tds_type + " for " + seller_sku,
									}
								)

						for promotion in promotion_list:
							amount = promotion.get("PromotionAmount", {}).get("CurrencyAmount", 0)
							promotion_discount += float(amount)

			charges_and_fees["principal_amounts"] = principal_amounts
			charges_and_fees["additional_discount"] = promotion_discount

			for service_fee in service_fee_event_list:
				if service_fee:
					for service_fee_item in service_fee.get("FeeList", []):
						fee_type = service_fee_item.get("FeeType")
						amount = service_fee_item.get("FeeAmount", {}).get("CurrencyAmount", 0)
						if float(amount) != 0:
							fee_account = self.get_account(fee_type)
							charges_and_fees.get("service_fees").append(
								{
									"charge_type": "Actual",
									"account_head": fee_account,
									"tax_amount": amount,
									"description": fee_type + " for " + seller_sku,
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

	def create_item(self, order_item) -> str:
		def create_item_group(amazon_item) -> str:
			if not amazon_item:
				return self.amz_setting.parent_item_group
			if not amazon_item.get("AttributeSets"):
				return self.amz_setting.parent_item_group
			item_group_name = amazon_item.get("AttributeSets")[0].get("ProductGroup")

			if item_group_name:
				item_group = frappe.db.get_value("Item Group", filters={"item_group_name": item_group_name})

				if not item_group:
					new_item_group = frappe.new_doc("Item Group")
					new_item_group.item_group_name = item_group_name
					new_item_group.parent_item_group = self.amz_setting.parent_item_group
					new_item_group.insert()
					return new_item_group.item_group_name
				return item_group

			raise (KeyError("ProductGroup"))

		def create_brand(amazon_item) -> str:
			if not amazon_item:
				return
			if not amazon_item.get("AttributeSets"):
				return

			brand_name = amazon_item.get("AttributeSets")[0].get("Brand")

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
			if not amazon_item.get("AttributeSets"):
				return
	  
			manufacturer_name = amazon_item.get("AttributeSets")[0].get("Manufacturer")

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
			if not amazon_item.get("AttributeSets"):
				return
	  
			item_price = frappe.new_doc("Item Price")
			item_price.price_list = self.amz_setting.price_list
			item_price.price_list_rate = (
				amazon_item.get("AttributeSets")[0].get("ListPrice", {}).get("Amount") or 0
			)
			item_price.item_code = item_code
			item_price.insert()

		catalog_items = self.get_catalog_items_instance()
		amazon_item = catalog_items.get_catalog_item(order_item["ASIN"])["payload"]

		item = frappe.new_doc("Item")
		item.item_group = create_item_group(amazon_item)
		item.brand = create_brand(amazon_item)
		item.manufacturer = create_manufacturer(amazon_item)
		item.amazon_item_code = order_item["SellerSKU"]
		item.item_code = order_item["SellerSKU"]
		item.item_name = order_item["SellerSKU"]
		item.description = order_item["Title"]
		item.insert(ignore_permissions=True)

		create_item_price(amazon_item, item.item_code)

		return item.name

	def get_item_code(self, order_item) -> str:
		if frappe.db.exists('Item', { 'amazon_item_code': order_item['SellerSKU']}):
			return frappe.db.get_value('Item', { 'amazon_item_code': order_item['SellerSKU']})

		item_code = self.create_item(order_item)
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

		while True:
			order_items_list = order_items_payload.get("OrderItems")
			next_token = order_items_payload.get("NextToken")

			for order_item in order_items_list:
				if order_item.get("QuantityOrdered") >= 0:
					item_amount = float(order_item.get("ItemPrice", {}).get("Amount", 0))
					item_tax = float(order_item.get("ItemTax", {}).get("Amount", 0))
					# shipping_price = float(order_item.get("ShippingPrice", {}).get("Amount", 0))
					# shipping_discount = float(order_item.get("ShippingDiscount", {}).get("Amount", 0))
					total_order_value = item_amount+item_tax
					item_qty = float(order_item.get("QuantityOrdered", 0))
					# In case of Cancelled orders Qty will be 0, Invoice will not get created
					if not item_qty:
						item_qty = 1
					item_rate = item_amount/item_qty
					item_code = self.get_item_code(order_item)
					actual_item = frappe.db.get_value("Item", item_code, "actual_item")
					if actual_item:
						item_code = actual_item
					final_order_items.append(
						{
							"item_code": item_code,
							"item_name": order_item.get("SellerSKU"),
							"description": order_item.get("Title"),
							"rate": item_rate,
							"base_rate": item_rate,
							"qty": item_qty,
							"amount": item_rate*item_qty,
							"base_amount": item_rate*item_qty,
							"uom": "Nos",
							"stock_uom": "Nos",
							"warehouse": warehouse,
							"conversion_factor": 1.0,
							"allow_zero_valuation_rate": 1,
							"total_order_value": total_order_value
						}
					)

			if not next_token:
				break

			order_items_payload = self.call_sp_api_method(
				sp_api_method=orders.get_order_items, order_id=order_id, next_token=next_token,
			)

		return final_order_items

	def create_sales_order(self, order) -> str | None:
		def create_customer(order) -> str:
			order_customer_name = order.get('AmazonOrderId', "")

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
						"links", {"link_doctype": "Customer", "link_name": existing_customer_name},
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
				new_contact.append("links", {"link_doctype": "Customer", "link_name": new_customer.name})

				new_contact.insert()

				return new_customer.name

		def create_address(order, customer_name) -> str | None:
			shipping_address = order.get("ShippingAddress")

			if not shipping_address:
				return
			else:
				make_address = frappe.new_doc("Address")
				make_address.address_line1 = shipping_address.get("AddressLine1", "Not Provided")
				make_address.city = shipping_address.get("City", "Not Provided")
				amazon_state = shipping_address.get("StateOrRegion")
				if frappe.db.get_single_value("Amazon SP API Settings", "map_state_data"):
					if frappe.db.exists("Amazon State Mapping", {"amazon_state": amazon_state}):
						make_address.state = frappe.db.get_value("Amazon State Mapping", {"amazon_state": amazon_state}, "state")
					else:
						failed_sync_record = frappe.new_doc('Amazon Failed Sync Record')
						failed_sync_record.amazon_order_id = order_id
						failed_sync_record.remarks = 'No State Mapping found for {0}'.format(amazon_state)
						failed_sync_record.save(ignore_permissions=True)
						return
				else:
					make_address.state = amazon_state
				make_address.pincode = shipping_address.get("PostalCode")

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

				make_address.append("links", {"link_doctype": "Customer", "link_name": customer_name})
				make_address.address_type = "Shipping"
				make_address.insert()

		def get_refunds(self, order_id, order_date, amazon_order_amount=0) -> dict:
			finances = self.get_finances_instance()
			financial_events_payload = self.call_sp_api_method(
				sp_api_method=finances.list_financial_events_by_order_id, order_id=order_id
			)
   
			if not (
				financial_events_payload
				and financial_events_payload.get("FinancialEvents")
				and financial_events_payload["FinancialEvents"].get("RefundEventList")
			):
				return []
   
			refund_events = []

			while True:
				shipment_event_list = financial_events_payload.get("FinancialEvents", {}).get("ShipmentEventList", [])
				refund_event_list = financial_events_payload.get("FinancialEvents", {}).get("RefundEventList", [])
				service_fee_event_list = financial_events_payload.get("FinancialEvents", {}).get("ServiceFeeEventList", [])
				next_token = financial_events_payload.get("NextToken")


				seller_sku = ''
				for refund_event in refund_event_list:
					if refund_event:

						charges_and_fees = {"posting_date": "", "items":[], "charges": [], "fees": [], "tds":[], "amazon_order_amount":amazon_order_amount, "order_date":order_date}

						charges_and_fees["posting_date"] = format_date_time_to_ist(refund_event.get("PostedDate"))
						for refund_item in refund_event.get("ShipmentItemAdjustmentList", []):
							charges = refund_item.get("ItemChargeAdjustmentList", [])
							fees = refund_item.get("ItemFeeAdjustmentList", [])
							promotions = refund_item.get("PromotionAdjustmentList", [])
							seller_sku = refund_item.get("SellerSKU")
							item_code = None
							if frappe.db.exists('Item', { 'amazon_item_code': seller_sku }):
								item_code =  frappe.db.get_value('Item', { 'amazon_item_code': seller_sku })

							for charge in charges:
								charge_type = charge.get("ChargeType")
								amount = charge.get("ChargeAmount", {}).get("CurrencyAmount", 0)

								if charge_type != "Principal" and float(amount) != 0:
									charge_account = self.get_account(charge_type)
									charges_and_fees.get("charges").append(
										{
											"charge_type": "Actual",
											"account_head": charge_account,
											"tax_amount": amount,
											"description": charge_type + " refund for " + seller_sku,
										}
									)
								else:
									charges_and_fees.get("items").append(
										{
											"item_code": item_code,
											"qty": refund_item.get("QuantityShipped"),
											"amount": charge.get("ChargeAmount", {}).get("CurrencyAmount", 0)
										}
									)

							for fee in fees:
								fee_type = fee.get("FeeType")
								amount = fee.get("FeeAmount", {}).get("CurrencyAmount", 0)

								if float(amount) != 0:
									fee_account = self.get_account(fee_type)
									charges_and_fees.get("fees").append(
										{
											"charge_type": "Actual",
											"account_head": fee_account,
											"tax_amount": amount,
											"description": fee_type + " refund for " + seller_sku,
										}
									)

							for promotion in promotions:
								promotion_type = promotion.get("PromotionType")
								amount = promotion.get("PromotionAmount", {}).get("CurrencyAmount", 0)

								if float(amount) != 0:
									promotion_account = self.get_account(promotion_type)
									charges_and_fees.get("fees").append(
										{
											"charge_type": "Actual",
											"account_head": promotion_account,
											"tax_amount": amount,
											"description": promotion_type + " refund for " + seller_sku,
										}
									)

						refund_events.append(charges_and_fees)			

				for service_fee in service_fee_event_list:
					if service_fee:

						charges_and_fees = {"posting_date": "", "items":[], "charges": [], "fees": [], "tds":[], "amazon_order_amount":amazon_order_amount, "order_date":order_date}

						for service_fee_item in service_fee.get("FeeList", []):
							fee_type = service_fee_item.get("FeeType")
							amount = service_fee_item.get("FeeAmount", {}).get("CurrencyAmount", 0)
							if float(amount) != 0:
								fee_account = self.get_account(fee_type)
								charges_and_fees.get("fees").append(
									{
										"charge_type": "Actual",
										"account_head": fee_account,
										"tax_amount": amount,
										"description": fee_type + " for " + seller_sku,
									}
								)
						refund_events.append(charges_and_fees)		
				
				tdss = []
				for shipment_event in shipment_event_list:
					if shipment_event:

						charges_and_fees = {"posting_date": "", "items":[], "charges": [], "fees": [], "tds":[], "amazon_order_amount":amazon_order_amount, "order_date":order_date}

						for shipment_item in shipment_event.get("ShipmentItemList", []):
							tds_list = shipment_item.get("ItemTaxWithheldList", [])
							if tds_list:
								tdss = tds_list[0].get("TaxesWithheld", [])
							for tds in tdss:
								tds_type = tds.get("ChargeType")
								amount = tds.get("ChargeAmount", {}).get("CurrencyAmount", 0)
								if float(amount) != 0:
									tds_account = self.get_account(tds_type)
									charges_and_fees.get("tds").append(
										{
											"charge_type": "Actual",
											"account_head": tds_account,
											"tax_amount": amount,
											"description": tds_type + " for " + seller_sku,
										}
									)
						refund_events.append(charges_and_fees)			

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
		so_id = None
		so_docstatus = 0
		refunds = get_refunds(self, order_id, order_date, amazon_order_amount)
		
		items = self.get_order_items(order_id)
		if frappe.db.exists("Sales Order", {"amazon_order_id": order_id}):
			so_id, so_docstatus = frappe.db.get_value("Sales Order", filters={"amazon_order_id": order_id}, fieldname=["name", "docstatus"])

		if so_id and refunds and so_docstatus:
			for refund in refunds:
				return_created = False
				if not frappe.db.exists("Sales Invoice", { "amazon_order_id": order_id, "docstatus":1, "is_return":0 }):
					# Stock Ghosting Process: "Ghosting" refers to adjusting stock for an invoice that lacks sufficient stock, not creating phantom stock.
					if self.amz_setting.adjust_stock_for_returns:
						ghost_stock_si = frappe.db.exists("Sales Invoice", {"amazon_order_id": order_id, "is_return": 0})
						if ghost_stock_si:
							ghost_stock_si_doc = frappe.get_doc("Sales Invoice", ghost_stock_si)

							if ghost_stock_si_doc.posting_date < self.amz_setting.return_invoice_stock_adjustment_before:

								# Create Stock Entry
								stock_for_return_created = create_stock_entry(ghost_stock_si)
								if stock_for_return_created and ghost_stock_si_doc.docstatus == 0:
									try:
										ghost_stock_si_doc.submit()
									except Exception as e:
										frappe.log_error("Error submiting Invoice {0} for Order ID {1}".format(ghost_stock_si, order_id), str(e), "Sales Invoice")

					if not return_created:
						failed_sync_record = frappe.new_doc('Amazon Failed Sync Record')
						failed_sync_record.amazon_order_id = order_id
						failed_sync_record.remarks = 'Failed to create return Sales Invoice, Not able to find any Sales Invoice with this Amazon Order ID. Sales Order ID : {0}'.format(so_id)
						failed_sync_record.payload = refund
						if refund.get("posting_date"):
							failed_sync_record.posting_date = dateutil.parser.parse(refund.get("posting_date")).strftime("%Y-%m-%d")
						if refund.get("order_date"):
							failed_sync_record.amazon_order_date = dateutil.parser.parse(refund.get("order_date")).strftime("%Y-%m-%d")
						if refund.get("amazon_order_amount"):
							failed_sync_record.amazon_order_amount = refund.get("amazon_order_amount")
						failed_sync_record.save(ignore_permissions=True)
						break

				si = frappe.db.get_value("Sales Invoice", { "amazon_order_id": order_id, "docstatus":1, "is_return":0  })

				existing_returns = tuple(frappe.db.get_all("Sales Invoice", {"return_against":si}, pluck="name"))
				for item in refund.get("items", []):

					return_si = frappe.new_doc("Sales Invoice")
					try:
						if refund.get("posting_date"):
							posting_date = refund.get("posting_date")
							return_si.posting_date = getdate(posting_date)
							return_si.posting_time = get_datetime(posting_date).strftime("%H:%M:%S")
							return_si.set_posting_time = 1
					except:
						pass
					return_si.is_return = 1
					return_si.update_stock = 1
					return_si.return_against = si
					return_si.customer = frappe.db.get_value("Sales Invoice", si, "customer")
					return_si.set_warehouse = frappe.db.get_value("Sales Invoice", si, "set_warehouse")

					actual_item = frappe.db.get_value("Item", item.get('item_code'), "actual_item")
					if not actual_item:
						actual_item = item.get("item_code")
					returned_qty = 0
					for returned_si in existing_returns:
						existing_returned_qty = frappe.db.get_value("Sales Invoice Item", {"parent": returned_si, "item_code": actual_item}, "qty") or 0
						returned_qty += existing_returned_qty
					if frappe.db.exists("Sales Invoice Item", {"parent": si, "item_code": actual_item}):
						if frappe.db.get_value("Sales Invoice Item", {"parent": si, "item_code": actual_item}, "qty") >= (returned_qty + float(item.get('qty'))):
							return_si.append("items", {
								"item_code": actual_item,
								"qty": -1 * float(item.get('qty')),
								"rate": abs(float(item.get('amount'))/float(item.get('qty'))),
								"sales_order": so_id,
								"sales_invoice_item": frappe.db.get_value("Sales Invoice Item", {"parent": si, "item_code": actual_item}, "name")
							})
							frappe.db.set_value("Sales Invoice Item", {"parent": si, "item_code": actual_item}, "refunded", 1)
							return_created = True

							if return_created:
								for charge in refund.get("charges", []):
									return_si.append("taxes", charge)

								for fee in refund.get("fees", []):
									return_si.append("taxes", fee)

								return_si.amazon_order_id = frappe.db.get_value("Sales Invoice", si, "amazon_order_id")
								return_si.disable_rounded_total = 1
								return_si.update_outstanding_for_self = 1
								return_si.update_billed_amount_in_sales_order = 1
								try:
									return_si.insert(ignore_permissions=True)
									return_si.submit()
								except Exception as e:
									frappe.log_error("Error creating Return Invoice for {0}".format(return_si.amazon_order_id), e, "Sales Invoice")

			return so_id

		else:
			if so_docstatus and so_id:
				return so_id
			if not so_id:
				so = frappe.new_doc("Sales Order")
			else:
				so = frappe.get_doc('Sales Order', so_id)

			customer_name = create_customer(order)
			create_address(order, customer_name)

			delivery_date = format_date_time_to_ist(order.get("LatestShipDate"))
			transaction_date = format_date_time_to_ist(order.get("PurchaseDate"))

			so.amazon_order_id = order_id
			so.marketplace_id = order.get("MarketplaceId")
			so.amazon_order_status = order.get("OrderStatus")
			so.fulfillment_channel = order.get("FulfillmentChannel")
			so.replaced_order_id = order.get("ReplacedOrderId") or ''
			if amazon_order_amount:
				so.amazon_order_amount = amazon_order_amount
			so.amazon_order_status = order.get("OrderStatus")
			so.customer = customer_name
			so.delivery_date = delivery_date if getdate(delivery_date) > getdate(transaction_date) else transaction_date
			so.transaction_date = get_datetime(transaction_date).strftime('%Y-%m-%d')
			so.transaction_time = get_datetime(transaction_date).strftime('%H:%M:%S')
			so.company = self.amz_setting.company
			warehouse = self.amz_setting.warehouse
			if so.fulfillment_channel:
				if so.fulfillment_channel=='AFN':
					warehouse = self.amz_setting.afn_warehouse
			if self.amz_setting.temporary_stock_transfer_required:
				warehouse = self.amz_setting.temporary_order_warehouse
			if order.get("IsBusinessOrder"):
				so.amazon_customer_type = 'B2B'
			else:
				so.amazon_customer_type = 'B2C'
			so.set_warehouse = warehouse

			items = self.get_order_items(order_id)

			if not items:
				if not so_id:
					return
				else:
					so.flags.ignore_mandatory = True
					so.disable_rounded_total = 1
					so.custom_validate()
					if so.grand_total>=0:
						so.save(ignore_permissions=True)
					else:
						remarks = 'Failed to create Sales Order for {0}. Sales Order grand Total = {1}'.format(order_id, so.grand_total)
						failed_sync_record = frappe.new_doc('Amazon Failed Sync Record')
						failed_sync_record.amazon_order_id = order_id
						failed_sync_record.remarks = remarks
						failed_sync_record.payload = so.as_dict()
						failed_sync_record.replaced_order_id = so.replaced_order_id
						failed_sync_record.posting_date = so.transaction_date
						failed_sync_record.amazon_order_date = so.transaction_date
						failed_sync_record.grand_total = so.grand_total
						failed_sync_record.amazon_order_amount = so.amazon_order_amount
						if not frappe.db.exists('Amazon Failed Sync Record', { 'amazon_order_id':order_id, 'remarks':remarks, 'grand_total':so.grand_total }):
							failed_sync_record.save(ignore_permissions=True)
					return

			so.items = []
			so.taxes = []
			so.taxes_and_charges = ''
			total_order_value = 0

			for item in items:
				total_order_value += item.get('total_order_value', 0)
				item["warehouse"] = warehouse
				so.append("items", item)

			if total_order_value:
				so.amazon_order_amount = total_order_value

			taxes_and_charges = self.amz_setting.taxes_charges
   
			item_lookup = {item["item_code"]: item.get('total_order_value', 0) for item in items}
			for row in so.items:
				total_value = item_lookup.get(row.item_code)
				if total_value:
					row.total_order_value = total_value

			if taxes_and_charges:
				charges_and_fees = self.get_charges_and_fees(order_id)
				if charges_and_fees.get("principal_amounts"):
					principal_amounts = charges_and_fees.get("principal_amounts")
					for item_row in so.items:
						if item_row.item_name and principal_amounts.get(item_row.item_name):
							pricipal_amount = float(principal_amounts.get(item_row.item_name)) or 0
							qty = item_row.qty
							if pricipal_amount:
								item_row.rate = pricipal_amount
								item_row.base_rate = pricipal_amount
								item_row.amount = pricipal_amount*qty
								item_row.base_amount = pricipal_amount*qty

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
					for service_fee in charges_and_fees.get("service_fees"):
						if service_fee:
							so.append("taxes", service_fee)

				if charges_and_fees.get("additional_discount"):
					so.discount_amount = float(charges_and_fees.get("additional_discount")) * -1

			so.flags.ignore_mandatory = True
			so.disable_rounded_total = 1
			so.custom_validate()
			if so.grand_total>=0:
				try:
					so.save(ignore_permissions=True)
				except Exception as e:
					frappe.log_error("Error saving Sales Order for Order {0}".format(so.amazon_order_id), e, "Sales Order")

				order_statuses = [
					"Shipped",
					"InvoiceUnconfirmed",
					"Unfulfillable",
				]

				if order.get("OrderStatus") in order_statuses:
					try:
						so.submit()
					except Exception as e:
						frappe.log_error("Error submitting Sales Order for Order {0}".format(so.amazon_order_id), e, "Sales Order")
			else:
				remarks = 'Failed to create Sales Order for {0}. Sales Order grand Total = {1}'.format(order_id, so.grand_total)
				failed_sync_record = frappe.new_doc('Amazon Failed Sync Record')
				failed_sync_record.amazon_order_id = order_id
				failed_sync_record.remarks = remarks
				failed_sync_record.replaced_order_id = so.replaced_order_id
				failed_sync_record.posting_date = so.transaction_date
				failed_sync_record.amazon_order_date = so.transaction_date
				failed_sync_record.grand_total = so.grand_total
				failed_sync_record.amazon_order_amount = so.amazon_order_amount
				if not so_id:
					failed_sync_record.payload = so.as_dict()
				if not frappe.db.exists('Amazon Failed Sync Record', { 'amazon_order_id':order_id, 'grand_total':so.grand_total, 'remarks':remarks }):
					failed_sync_record.save(ignore_permissions=True)

			return so.name

	def get_orders(self, last_updated_after, sync_selected_date_only=0) -> list:
		orders = self.get_orders_instance()
		order_statuses = [
			"Shipped",
			"InvoiceUnconfirmed",
			"Canceled",
			"Unfulfillable",
		]
		fulfillment_channels = ["FBA", "SellerFulfilled"]
		if sync_selected_date_only:
			last_updated_before = add_days(getdate(last_updated_after), 1).strftime( "%Y-%m-%d")
			orders_payload = self.call_sp_api_method(
				sp_api_method=orders.get_orders,
				last_updated_after=last_updated_after,
				last_updated_before=last_updated_before,
				order_statuses=order_statuses,
				fulfillment_channels=fulfillment_channels,
				max_results=50,
			)
		else:
			orders_payload = self.call_sp_api_method(
				sp_api_method=orders.get_orders,
				last_updated_after=last_updated_after,
				order_statuses=order_statuses,
				fulfillment_channels=fulfillment_channels,
				max_results=50,
			)

		sales_orders = []

		while True:
			if orders_payload:
				orders_list = orders_payload.get("Orders")
				next_token = orders_payload.get("NextToken")
				if not orders_list or len(orders_list) == 0:
					break
				for order in orders_list:
					sales_order = self.create_sales_order(order)
					if sales_order:
						sales_orders.append(sales_order)
				if not next_token:
					break
				orders_payload = self.call_sp_api_method(
					sp_api_method=orders.get_orders, last_updated_after=last_updated_after, next_token=next_token,
				)
		frappe.enqueue("eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings.enq_si_submit", sales_orders=sales_orders)
		return sales_orders

	def get_order(self, amazon_order_ids) -> list:
		orders = self.get_orders_instance()
		order_payload = self.call_sp_api_method(
			sp_api_method=orders.get_order,
			order_id=amazon_order_ids,
		)
		sales_orders = []
		if order_payload:
			try:
				sales_order = self.create_sales_order(order_payload)
				if sales_order:
					sales_orders.append(sales_order)
			except:
				pass
		# frappe.enqueue("eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings.enq_si_submit", sales_orders=sales_orders)
		return sales_orders

	def get_catalog_items_instance(self) -> CatalogItems:
		return CatalogItems(**self.instance_params)

def get_orders(amz_setting_name, last_updated_after, sync_selected_date_only=0) -> list:
	ar = AmazonRepository(amz_setting_name)
	return ar.get_orders(last_updated_after, sync_selected_date_only)

@frappe.whitelist()
def get_order(amz_setting_name, amazon_order_ids) -> list:
	ar = AmazonRepository(amz_setting_name)
	return ar.get_order(amazon_order_ids)

@frappe.whitelist()
def create_stock_entry(sales_invoice):
	'''
		Method to create Stock entry for Returns and Replaced Orders
	'''
	stock_entry_created = False
	if frappe.db.exists('Sales Invoice', sales_invoice):
		si_doc = frappe.get_doc('Sales Invoice', sales_invoice)
		stock_entry = frappe.new_doc('Stock Entry')
		stock_entry.update({
			"stock_entry_type": "Material Receipt",
			"set_posting_time": 1,
			"posting_date": si_doc.posting_date,
			"posting_time": si_doc.posting_time,
			"sales_invoice_no": si_doc.name,
			"from_return_invoice": 1,
			"remarks": "Stock updated to reflect return/replacement for Amazon Order {0}".format(si_doc.amazon_order_id),
			"to_warehouse": si_doc.set_warehouse
		})

		#Setting Items
		for item in si_doc.items:
			stock_entry.append("items", {
				"item_code": item.item_code,
				"qty": item.qty,
				"allow_zero_valuation_rate":1
			})

		# Savepoint for rollback safety
		frappe.db.savepoint("ghost_stocking")
		try:
			stock_entry.insert(ignore_permissions=True)
			stock_entry.submit()
			stock_entry_created = True
		except Exception as e:
			stock_entry_created = False
			frappe.db.rollback(save_point="ghost_stocking")
			frappe.get_doc({
				"doctype": "Amazon Failed Invoice Record",
				"invoice_id": sales_invoice,
				"error": str(e),
			}).insert()
	return stock_entry_created

@frappe.whitelist()
def create_stock_entries_for_sis(names):
	"""method to bulk create stock entries for draft sales invoices
	"""
	names = json.loads(names)
	for name in names:
		create_stock_entry(name)
