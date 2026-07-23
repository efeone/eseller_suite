# Copyright (c) 2026, efeone and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class STNToDoLog(Document):
	def set_references(self):
		''''
			Method to set Refernce Sales and Purchase Invoice
		'''
		if self.invoice_number:
			#Setting Sales Invoice reference
			if not self.sales_invoice:
				si = frappe.db.get_value('Sales Invoice', { 'amazon_invoice_id':self.invoice_number, 'is_return':0 })
				if si:
					self.sales_invoice = si

			#Setting Purchase Invoice reference
			if not self.purchase_invoice:
				pi = frappe.db.get_value('Purchase Invoice', { 'amazon_invoice_id':self.invoice_number, 'is_return':0 })
				if pi:
					self.purchase_invoice = pi
	
	def before_save(self):
		self.set_references()
	
	def on_update(self):
		if self.credit_note and self.debit_note:
			frappe.db.set_value('Amazon STN Entry Item', self.stn_entry_item, 'transactions_created', 1, update_modified=False)
			frappe.db.set_value('Amazon STN Entry Item', self.stn_entry_item, 'error_log', '', update_modified=False)
