# Copyright (c) 2025, efeone and contributors
# For license information, please see license.txt

import frappe

def before_save(doc, method):
	'''
		Method which trigger on before_save event of Purchase Invoice
	'''
	set_bundle_diff_amount(doc)
	set_discount_based_on_amazon_value(doc)

def on_submit(doc, method):
	'''
		Method which trigger on on_submit event of Purchase Invoice
	'''
	if doc.bundle_difference_amount >=1 or doc.bundle_difference_amount <= -1:
		title = 'Check Difference Amount'
		msg = 'Cannot submit the invoice due to difference amount of {0} for bundle items'.format(frappe.bold(doc.bundle_difference_amount))
		frappe.throw(
			title=title,
			msg=msg
		)
	if doc.amazon_invoice_id:
		unset_stn_exception(doc)

def on_update_after_submit(doc, method):
	'''
		Method which trigger on on_update_after_submit event of Purchase Invoice
	'''
	unset_stn_exception(doc)

def on_cancel(doc, method):
	'''
		Method which trigger on on_cancel event of Purchase Invoice
	'''
	doc.ignore_linked_doctypes = (
		"GL Entry",
		"Stock Ledger Entry",
		"Repost Item Valuation",
		"Repost Payment Ledger",
		"Repost Payment Ledger Items",
		"Repost Accounting Ledger",
		"Repost Accounting Ledger Items",
		"Unreconcile Payment",
		"Unreconcile Payment Entries",
		"Payment Ledger Entry",
		"Tax Withheld Vouchers",
		"Serial and Batch Bundle",
		"Amazon STN Entry",
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
		if item.rate and item.qty:
			item.amount = item.rate * item.qty
		total_bundle_amount_actual += item.amount
	doc.total_bundle_amount_actual = round(total_bundle_amount_actual, 2)

	# Setting Difference Amount
	doc.bundle_difference_amount = round((doc.total_bundle_amount_actual - doc.total_bundle_amount), 2)

def set_discount_based_on_amazon_value(doc):
	'''
		Method to set dicount based on Amazon value and outstanding amount
	'''
	if doc.amazon_invoice_value and doc.outstanding_amount:
		diff = doc.outstanding_amount - doc.amazon_invoice_value
		# Add discount if difference is between -1 to 1, else it may be some error
		if diff and (1 > diff > -1):
			doc.apply_discount_on = 'Grand Total'
			doc.discount_amount = diff

def unset_stn_exception(doc):
	'''
	Method to unset STN exception after Purchase Invoice submission
	'''
	stn_items = frappe.get_all(
		'Amazon STN Entry Item', filters={'purchase_invoice': doc.name}, fields=['name', 'sales_invoice']
	)

	if not stn_items:
		return

	sales_invoices = set()

	# Clear error logs and collect unique Sales Invoices
	for item in stn_items:
		frappe.db.set_value('Amazon STN Entry Item', item.name, 'error_log', '', update_modified=False)
		if item.sales_invoice:
			sales_invoices.add(item.sales_invoice)
	frappe.db.commit()

	# Submit each Sales Invoice only once
	for si_ref in sales_invoices:
		if frappe.db.get_value('Sales Invoice', si_ref, 'docstatus') == 0:
			try:
				frappe.get_doc('Sales Invoice', si_ref).submit()
			except Exception as e:
				frappe.db.rollback()
				exception_msg = f"Failed to submit Sales Invoice: {si_ref} - {str(e)}"

				# Update all related STN items with the error
				for item in stn_items:
					if item.sales_invoice == si_ref:
						frappe.db.set_value('Amazon STN Entry Item', item.name, 'error_log', exception_msg, update_modified=False)
				frappe.db.commit()
