# Copyright (c) 2024, efeone and contributors
# For license information, please see license.txt

import re

import frappe
import json
from frappe.model.document import Document
from frappe.utils import get_url_to_form

from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_repository import get_order
from eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings import enhance_hsn_error_with_items
from eseller_suite.eseller_suite.custom_script.sales_order.sales_order import make_sales_invoice, get_account_head

class AmazonFailedSyncRecord(Document):
	@frappe.whitelist()
	def retry_fetching(self):
		so = []
		if self.amazon_order_id:
			if frappe.db.exists('Amazon SP API Settings', { 'is_active':1 }):
				amz_setting_name = frappe.db.get_value('Amazon SP API Settings', { 'is_active':1 })
				try:
					so = get_order(amz_setting_name=amz_setting_name, amazon_order_ids=self.amazon_order_id)
					# Check if order/invoice was created successfully
					if so and len(so) > 0:
						# Verify that a Sales Order or Sales Invoice exists for this amazon_order_id
						so_exists = frappe.db.exists("Sales Order", {"amazon_order_id": self.amazon_order_id})
						si_exists = frappe.db.exists("Sales Invoice", {"amazon_order_id": self.amazon_order_id})

						if so_exists or si_exists:
							# Order/Invoice created successfully, delete the failed sync record
							record_name = self.name
							frappe.delete_doc(self.doctype, record_name, ignore_permissions=True, force=True)
							return {"success": True, "message": "Order/Invoice created successfully. Failed sync record deleted."}
				except Exception as e:
					error_msg = str(e)
					# Check if it's an HSN/SAC error and enhance with item information
					if "HSN/SAC" in error_msg or "hsn_code" in error_msg.lower():
						# Try to get the sales order or sales invoice to extract item information
						try:
							# Check for Sales Order first
							so_name = frappe.db.get_value("Sales Order", {"amazon_order_id": self.amazon_order_id}, "name")
							if so_name:
								doc = frappe.get_doc("Sales Order", so_name)
								enhanced_error = enhance_hsn_error_with_items(error_msg, doc)
								# Update the failed sync record with enhanced error
								self.remarks = enhanced_error
								self.save(ignore_permissions=True)
								# Re-throw with enhanced message
								frappe.throw(enhanced_error)
							else:
								# Check for Sales Invoice
								si_name = frappe.db.get_value("Sales Invoice", {"amazon_order_id": self.amazon_order_id, "docstatus": 0}, "name")
								if si_name:
									doc = frappe.get_doc("Sales Invoice", si_name)
									enhanced_error = enhance_hsn_error_with_items(error_msg, doc)
									# Update the failed sync record with enhanced error
									self.remarks = enhanced_error
									self.save(ignore_permissions=True)
									# Re-throw with enhanced message
									frappe.throw(enhanced_error)
						except Exception:
							# If enhancement fails, just throw original error
							pass
					# Re-throw the original exception if not HSN error or enhancement failed
					raise
		if so:
			return so
		else:
			return 0

	@frappe.whitelist()
	def create_replaced_so(self):
		if self.payload and not self.replaced_so:
			data = json.loads(self.payload)
			so_doc = frappe.get_doc(data)
			so_doc.taxes = []
			so_doc.flags.ignore_mandatory = True
			so_doc.disable_rounded_total = 1
			so_doc.custom_validate()
			so_doc.save(ignore_permissions=True)
			if so_doc.amazon_order_status == 'Shipped':
				so_doc.submit()
			frappe.db.set_value(self.doctype, self.name, 'replaced_so', so_doc.name)
			frappe.msgprint('Sales Order Created: <a href="{0}">{1}</a>'.format(get_url_to_form(so_doc.doctype, so_doc.name), so_doc.name), alert=True, indicator='green')

	@frappe.whitelist()
	def create_replaced_jv(self):
		if self.payload and not self.replaced_jv and self.grand_total:
			data = json.loads(self.payload)
			jv_doc = frappe.new_doc('Journal Entry')
			jv_doc.voucher_type = 'Journal Entry'
			jv_doc.posting_date = self.posting_date
			jv_doc.user_remark = 'Adjustment Entry for Replaced Order'
			jv_doc.amazon_order_id = self.amazon_order_id
			if data.get('taxes'):
				for row in data.get('taxes'):
					tax_amount = abs(float(row.get('tax_amount')))
					jv_row = jv_doc.append('accounts')
					jv_row.account = row.get('account_head')
					jv_row.debit = tax_amount
					jv_row.debit_in_account_currency = tax_amount
					jv_row.user_remark = row.get('description')
					jv_row.amazon_order_id = self.amazon_order_id
			if data.get('customer') and data.get('company'):
				default_receivable_account = frappe.db.get_value('Company', data.get('company'), 'default_receivable_account')
				jv_row = jv_doc.append('accounts')
				jv_row.credit = abs(self.grand_total)
				jv_row.credit_in_account_currency = abs(self.grand_total)
				jv_row.user_remark = 'Adjustment Entry for Replaced Order'
				jv_row.amazon_order_id = self.amazon_order_id
				jv_row.party_type = 'Customer'
				jv_row.party = data.get('customer')
				jv_row.account = default_receivable_account
				jv_doc.flags.ignore_mandatory = True
				jv_doc.save(ignore_permissions=True)
				jv_doc.submit()
				frappe.db.set_value(self.doctype, self.name, 'replaced_jv', jv_doc.name)
				frappe.msgprint('Journal Entry Created: <a href="{0}">{1}</a>'.format(get_url_to_form(jv_doc.doctype, jv_doc.name), jv_doc.name), alert=True, indicator='green')

	@frappe.whitelist()
	def create_adjustment_jv(self):
		'''
			Adjustment JV in case of -ve orders, due to high charges and less invoice value
		'''
		data = json.loads(self.payload)
		jv_doc = frappe.new_doc('Journal Entry')
		jv_doc.voucher_type = 'Journal Entry'
		jv_doc.posting_date = self.posting_date
		jv_doc.user_remark = 'Adjustment Entry for Replaced Order'
		jv_doc.amazon_order_id = self.amazon_order_id
		total_tax_amount = 0
		if data.get('taxes'):
			for row in data.get('taxes'):
				if float(row.get('tax_amount'))<0:
					tax_amount = abs(float(row.get('tax_amount')))
					total_tax_amount += tax_amount
					jv_row = jv_doc.append('accounts')
					jv_row.account = row.get('account_head')
					jv_row.debit = tax_amount
					jv_row.debit_in_account_currency = tax_amount
					jv_row.user_remark = row.get('description')
					jv_row.amazon_order_id = self.amazon_order_id
		if data.get('customer') and data.get('company') and total_tax_amount:
			default_receivable_account = frappe.db.get_value('Company', data.get('company'), 'default_receivable_account')
			jv_row = jv_doc.append('accounts')
			jv_row.credit = total_tax_amount
			jv_row.credit_in_account_currency = total_tax_amount
			jv_row.user_remark = 'Adjustment Entry for -ve Order Value'
			jv_row.amazon_order_id = self.amazon_order_id
			jv_row.party_type = 'Customer'
			jv_row.party = data.get('customer')
			jv_row.account = default_receivable_account
			jv_doc.flags.ignore_mandatory = True
			jv_doc.save(ignore_permissions=True)
			jv_doc.submit()
			frappe.msgprint('Journal Entry Created: <a href="{0}">{1}</a>'.format(get_url_to_form(jv_doc.doctype, jv_doc.name), jv_doc.name), alert=True, indicator='green')

	@frappe.whitelist()
	def create_adjustment_so(self):
		data = json.loads(self.payload)
		so_doc = frappe.get_doc(data)
		so_doc.taxes = []
		for row in data.get('taxes'):
			if float(row.get('tax_amount'))>0:
				so_doc.append('taxes', row)
		so_doc.flags.ignore_mandatory = True
		so_doc.disable_rounded_total = 1
		so_doc.custom_validate()
		so_doc.save(ignore_permissions=True)
		if so_doc.amazon_order_status == 'Shipped':
			so_doc.submit()
		frappe.msgprint('Sales Order Created: <a href="{0}">{1}</a>'.format(get_url_to_form(so_doc.doctype, so_doc.name), so_doc.name), alert=True, indicator='green')

	@frappe.whitelist()
	def create_return_invoice(self):
		'''
			Method to create exceptional Credit Notes, without Sales Invoice
		'''
		so = frappe.db.get_value('Sales Order', { 'amazon_order_id':self.amazon_order_id }, 'name')
		data = json.loads(self.payload)
		if so:
			return_si = make_sales_invoice(source_name=so, target_doc=None, ignore_permissions=True)
			return_si.set_posting_time = 1
			return_si.posting_date = self.posting_date
			return_si.posting_time = frappe.utils.get_time(data.get('posting_date'))
			return_si.update_stock = 1
			return_si.is_return = 1
			# items
			return_si.items = []
			for row in data.get('items'):
				return_si.append('items', {
					'item_code': row.get('item_code'),
					'qty': abs(row.get('qty')) * -1,
					'rate': abs(row.get('amount'))/abs(row.get('qty')),
					'amount': abs(row.get('amount')) * -1,
					'sales_order': so,
				})
			# taxes and charges
			return_si.taxes = []
			for row in data.get('charges'):
				return_si.append('taxes', {
					'account_head': row.get('account_head'),
					'description': row.get('description'),
					'tax_amount': row.get('tax_amount'),
					'charge_type': row.get('charge_type'),
				})
			for row in data.get('fees'):
				return_si.append('taxes', {
					'account_head': row.get('account_head'),
					'description': row.get('description'),
					'tax_amount': row.get('tax_amount'),
					'charge_type': row.get('charge_type'),
				})
			for row in data.get('tds'):
				return_si.append('taxes', {
					'account_head': row.get('account_head'),
					'description': row.get('description'),
					'tax_amount': row.get('tax_amount'),
					'charge_type': row.get('charge_type'),
				})

			# Handling Company changes
			company_abr, default_cc = frappe.db.get_value('Company', return_si.company, ['abbr', 'cost_center'])
			for row in return_si.items:
				if row.cost_center:
					current_cc = row.cost_center
					new_cc = re.sub(r"-[^-]*$", f"- {company_abr}", current_cc)
					row.cost_center = new_cc if frappe.db.exists('Cost Center', new_cc) else default_cc
				if row.item_tax_template:
					current_tax_temp = row.item_tax_template
					new_tax_temp = re.sub(r"-[^-]*$", f"- {company_abr}", current_tax_temp)
					row.item_tax_template = new_tax_temp if frappe.db.exists('Item Tax Template', new_tax_temp) else ''
			return_si.packed_items = []

			for row in return_si.taxes:
				if row.cost_center:
					current_cc = row.cost_center
					new_cc = re.sub(r"-[^-]*$", f"- {company_abr}", current_cc)
					row.cost_center = new_cc if frappe.db.exists('Cost Center', new_cc) else default_cc
				if row.account_head:
					row.account_head = get_account_head(row.account_head, return_si.company)

			return_si.additional_discount_percentage = 0
			return_si.discount_amount = 0
			return_si.save(ignore_permissions=True)
			return_si.submit()

			#Close respective Sales Order
			frappe.db.set_value('Sales Order', so, 'status', 'Closed')
			frappe.msgprint('Credit Note Created: <a href="{0}">{1}</a>'.format(get_url_to_form(return_si.doctype, return_si.name), return_si.name), alert=True, indicator='green')
