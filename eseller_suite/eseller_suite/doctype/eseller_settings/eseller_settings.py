# Copyright (c) 2024, efeone and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

class eSellerSettings(Document):
	def validate(self):
		self.validate_parent_warehosue_defaults()
		self.validate_payment_defaults()
		self.set_mop_accounts()

	def validate_parent_warehosue_defaults(self):
		'''
			Validates that there are no duplicate companies in the Parent Warehouses table.
		'''
		companies = {row.company for row in self.parent_warehouses}

		if len(companies) != len(self.parent_warehouses):
			frappe.throw(_("Cannot set multiple Parent Warehouses for a company."))

	def validate_payment_defaults(self):
		'''
			Validates that there are no duplicate companies in the Payment Accounts table.
		'''
		companies = {row.company for row in self.payment_accounts}

		if len(companies) != len(self.payment_accounts):
			frappe.throw(_("Cannot set multiple Payment Accounts for a company."))

	def set_mop_accounts(self):
		'''
			Sets the account for each mode of payment based on the Payment Accounts table.
		'''
		for row in self.payment_accounts:
			filters = {"company": row.company, "parent": row.mode_of_payment}
			mop_account = frappe.get_value("Mode of Payment Account", filters, 'default_account')
			if mop_account != row.mop_account:
				row.mop_account = mop_account
			if self.use_reserve_lines_in_amazon_payment_entry != row.use_reserve_lines_in_amazon_payment_entry:
				row.use_reserve_lines_in_amazon_payment_entry = self.use_reserve_lines_in_amazon_payment_entry

@frappe.whitelist()
def get_all_companies():
	'''
		Returns a list of all companies in the system, ordered by name.
	'''
	return frappe.get_all("Company", fields=["name"], order_by="name asc")

@frappe.whitelist()
def get_naming_series_options(doctype):
	'''
		Returns a list of naming series options for a given doctype.
	'''
	options = frappe.get_meta(doctype).get_naming_series_options()
	series = [v for v in options if v and str(v).strip()]
	return series