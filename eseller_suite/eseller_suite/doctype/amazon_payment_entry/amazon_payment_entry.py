# Copyright (c) 2024, efeone and contributors
# For license information, please see license.txt

import csv
import os
from datetime import datetime

import frappe
from frappe import _
from charset_normalizer import from_path
from frappe.model.document import Document
from frappe.utils import get_url_to_form, flt
from frappe.core.doctype.submission_queue.submission_queue import queue_submission

from eseller_suite.eseller_suite.utils import is_old_data

# Fixed set of every account_field your _apply_expense_account_rules / _process_cancellation
# code can ask for. Prefetched once regardless of which rows actually need them --
# it's one cheap query either way.
ALL_ACCOUNT_FIELDS = [
	'inventory_reimbursement_account',
	'inventory_reimbursement_income_account',
	'other_expenses_account',
	'other_income_account',
	'shipping_charges_account',
	'reimbursement_income_account',
	'tds_reimbursement_account',
	'fba_flex_weight_account',
	'order_cancellation_account',
]


class AmazonPaymentEntry(Document):
	def before_save(self):
		'''
		Method to check if the return sales invoice is already cancelled
		and remove it from the payment details table
		'''
		for row in self.payment_details:
			if row.return_sales_invoice:
				if frappe.db.get_value('Sales Invoice', row.return_sales_invoice, 'docstatus') == 2:
					row.return_sales_invoice = ''
			if row.sales_invoice:
				if frappe.db.get_value('Sales Invoice', row.sales_invoice, 'docstatus') == 2:
					row.sales_invoice = ''
	 
	def validate(self):
		if not self.payment_details:
			self.process_payment_data()

	def submit(self):
		if len(self.payment_details) > 50:
			queue_submission(self, "_submit")
		else:
			return self._submit()

	def on_submit(self):
		self.validate_missing_invoices()
		frappe.db.set_value(self.doctype, self.name, 'in_progress', 1)
		self.create_journal_entry()

	def process_payment_data(self):
		if self.payment_file:
			attached_file = frappe.get_doc("File", {"file_url": self.payment_file})
			file_path = frappe.get_site_path("private", "files", attached_file.file_name)
			frappe.logger().debug(f"Processing file at path: {file_path}")
			if not os.path.exists(file_path):
				frappe.throw(f"File not found at path: {file_path}")
			if attached_file.file_url.endswith(".csv"):
				self.process_csv(file_path)
			else:
				frappe.throw("Unsupported file format. Only CSV files are supported.")
		else:
			self.payment_details = []

	def process_csv(self, file_path):
		try:
			detected = from_path(file_path).best()
			encoding = detected.encoding

			with open(file_path, "r", encoding=encoding) as file:
				csv_reader = csv.DictReader(file)
				for row in csv_reader:
					self.save_payment_details(row)
		except Exception as e:
			frappe.throw("Error Processing the file: {0}".format(e))

	def parse_date(self, date_str):
		try:
			parsed_date = datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
			return parsed_date
		except ValueError:
			frappe.throw(f"Invalid date format: {date_str}")

	def save_payment_details(self, row):
		key_mapping = {
		'Transaction type': "transaction_type",
		'Product Details': "product_details",
		'Order ID': "order_id",
		'Total (INR)': "total"
		}
		if row.get('\ufeff"Date"'):
			key_mapping['\ufeff"Date"'] = "date"
		else:
			key_mapping['Date'] = "date"
		payment_detail = {}
		is_blank_row = True
		for csv_key, internal_key in key_mapping.items():
			value = row.get(csv_key)
			if value:
				is_blank_row = False
				if internal_key == "date":
					value = self.parse_date(value)
				payment_detail[internal_key] = value
		if not is_blank_row and payment_detail.get('transaction_type'):
			self.append("payment_details", payment_detail)

	@frappe.whitelist()
	def fetch_invoice_details(self):
		'''
			Method to get fetch Invoice and Customer details against each Amazon Order IDs
		'''
		has_changes = False
		pending_rows = frappe.db.get_all('Amazon Payment Entry Item', {
			'parent': self.name, 'ready_to_process': 0, 'ignore_transaction': 0
		})

		order_ids = list({
			row.order_id for row in self.payment_details
			if row.order_id and row.order_id != '---'
		})
		service_type_names = list({
			row.product_details for row in self.payment_details
			if row.transaction_type == 'Service Fees' and row.product_details
		})

		# --- one round trip per data source, instead of one per row ---
		so_map = self._prefetch_sales_orders(order_ids)
		invoice_map = prefetch_invoice_details(order_ids, is_return=0)
		return_invoice_map = prefetch_invoice_details(order_ids, is_return=1)
		replaced_jv_map = prefetch_replaced_jvs(order_ids)
		service_type_map = prefetch_service_types(service_type_names)
		account_map, valid_account_fields = prefetch_payment_accounts(ALL_ACCOUNT_FIELDS)

		processed = 0
		for row in self.payment_details:
			if self._sync_sales_order_flag(row, so_map):
				has_changes = True

			if row.ready_to_process:
				continue

			processed += 1
			frappe.publish_realtime("fetch_invoice_details", dict(progress=processed, total=len(pending_rows)))
			row.company = self.company

			if self._process_primary_transaction(row, invoice_map, return_invoice_map, service_type_map):
				has_changes = True
			if self._process_order_payment_reversal(row, replaced_jv_map):
				has_changes = True
			if self._apply_expense_account_rules(row, account_map, valid_account_fields):
				has_changes = True
			if self._process_cancellation(row, account_map, valid_account_fields):
				has_changes = True

			if row.amazon_expense_account:
				row.company = frappe.db.get_value('Account', row.amazon_expense_account, 'company')

			if self._fallback_ready_to_process(row):
				has_changes = True

		if has_changes:
			self.save()

		return 1

	def _prefetch_sales_orders(self, order_ids):
		if not order_ids:
			return {}
		rows = frappe.db.get_all(
			'Sales Order',
			filters={'amazon_order_id': ['in', order_ids], 'docstatus': ['!=', 2]},
			fields=['amazon_order_id', 'transaction_date']
		)
		return {r.amazon_order_id: r.transaction_date for r in rows}

	def _sync_sales_order_flag(self, row, so_map):
		'''Mark row as having a Sales Order and flag old transactions to be ignored'''
		if row.order_id and not row.has_sales_order and row.order_id in so_map:
			row.has_sales_order = 1
			if is_old_data(so_map[row.order_id]):
				row.ignore_transaction = 1
			return True
		return False

	def _process_primary_transaction(self, row, invoice_map, return_invoice_map, service_type_map):
		'''Mutually exclusive: invoice-linked transactions, unavailable balance, or service fees'''
		if row.order_id and row.transaction_type in [
			'Order Payment', 'Amazon Easy Ship Charges', 'Fulfillment Fee Refund', 'Refund', 'Other'
		]:
			return self._process_invoice_details(row, invoice_map, return_invoice_map)
		elif row.transaction_type in ["Unavailable balance", "Previous statement's unavailable balance"]:
			row.ready_to_process = 1
			return True
		elif row.transaction_type == 'Service Fees':
			return self._process_service_fees(row, service_type_map)
		return False

	def _process_invoice_details(self, row, invoice_map, return_invoice_map):
		'''Resolve Sales Invoice / Customer / return details for order-linked transactions -- pure in-memory now'''
		has_changes = False
		is_return = (
			row.transaction_type in ['Fulfillment Fee Refund', 'Refund']
			or row.product_details == 'Weight Handling Fees Reversal'
		)

		invoice_details = resolve_invoice_from_candidates(invoice_map.get(row.order_id, []), row.total)
		return_invoice_details = (
			resolve_invoice_from_candidates(return_invoice_map.get(row.order_id, []), row.total)
			if is_return else None
		)

		if invoice_details.get('sales_invoice'):
			row.sales_invoice = invoice_details.get('sales_invoice')
			row.company = invoice_details.get('company')
		if invoice_details.get('customer'):
			row.customer = invoice_details.get('customer')
			if not is_return:
				row.ready_to_process = 1
			has_changes = True

		if return_invoice_details:
			if return_invoice_details.get('sales_invoice'):
				row.return_sales_invoice = return_invoice_details.get('sales_invoice')
				row.ready_to_process = 1
				has_changes = True
			if return_invoice_details.get('customer') and not row.customer:
				row.customer = return_invoice_details.get('customer')
				has_changes = True
			if return_invoice_details.get('company'):
				row.company = return_invoice_details.get('company')
				has_changes = True
			if return_invoice_details.get('debit_to'):
				row.default_receivable_account = return_invoice_details.get('debit_to')
				has_changes = True
		elif self.remaining_refunds_have_no_returns:
			row.ready_to_process = 1
			has_changes = True

		if row.transaction_type == 'Refund' and float(row.total) == 0:
			row.ready_to_process = 1
			has_changes = True

		return has_changes

	def _process_service_fees(self, row, service_type_map):
		if not row.product_details:
			return False
		expense_account = service_type_map.get(row.product_details)
		if expense_account is None and row.product_details not in service_type_map:
			return False  # Amazon Service Type doesn't exist at all
		has_changes = False
		if row.product_details in service_type_map:
			row.amazon_service_type = row.product_details
		if expense_account:
			row.amazon_expense_account = expense_account
			row.ready_to_process = 1
			has_changes = True
		return has_changes

	def _process_order_payment_reversal(self, row, replaced_jv_map):
		'''Negative Order Payment rows need a replaced JV or an existing invoice/customer before processing'''
		if not (row.transaction_type == 'Order Payment' and row.total and row.order_id):
			return False
		if float(row.total) >= 0:
			return False

		row.ready_to_process = 0
		replaced_jv = replaced_jv_map.get(row.order_id)
		if replaced_jv:
			row.journal_entry = replaced_jv
			if row.sales_invoice and row.customer:
				row.ready_to_process = 1
				return True
		elif row.sales_invoice and row.customer:
			row.ready_to_process = 1
			return True
		return False

	def _set_expense_account(self, row, account_field, account_map, valid_account_fields):
		'''Shared pattern: dict lookup instead of a query, marks row ready if found'''
		if account_field not in valid_account_fields:
			return False
		acct_row = account_map.get(row.company)
		if not acct_row:
			return False
		account = acct_row.get(account_field)
		if not account:
			return False
		row.ready_to_process = 1
		row.amazon_expense_account = account
		return True

	def _apply_expense_account_rules(self, row, account_map, valid_account_fields):
		has_changes = False
		total = float(row.total) if row.total else 0

		if row.transaction_type in ['Other', 'Inventory Reimbursement'] \
			and row.product_details in ['FBA Inventory Reimbursement', 'FBA Reversed Reimbursement'] \
			and row.order_id == '---':
			account_field = (
				'inventory_reimbursement_account' if total < 0
				else 'inventory_reimbursement_income_account' if total > 0
				else None
			)
			if account_field and self._set_expense_account(row, account_field, account_map, valid_account_fields):
				has_changes = True

		if row.transaction_type == 'Inventory Reimbursement' \
			and row.product_details == 'FBA Inventory Reimbursement' and row.order_id and total > 0:
			if self._set_expense_account(row, 'inventory_reimbursement_income_account', account_map, valid_account_fields):
				has_changes = True

		if row.transaction_type == 'Other' and row.product_details == 'Others' and row.order_id == '---':
			account_field = (
				'other_expenses_account' if total < 0
				else 'other_income_account' if total > 0
				else None
			)
			if account_field and self._set_expense_account(row, account_field, account_map, valid_account_fields):
				has_changes = True

		product_to_account = {
			'Easy Ship Shipping Fee': 'shipping_charges_account',
			'Reimbursement or lost or damaged package': 'reimbursement_income_account',
			'TDS Reimbursements': 'tds_reimbursement_account',
			'FBA Flex Weight Handling Fees': 'fba_flex_weight_account',
		}
		if row.transaction_type == 'Other' and row.product_details in product_to_account:
			if self._set_expense_account(row, product_to_account[row.product_details], account_map, valid_account_fields):
				has_changes = True

		return has_changes

	def _process_cancellation(self, row, account_map, valid_account_fields):
		if not (
			row.transaction_type.strip() == 'Cancellation'
			and row.product_details == 'Order Cancellation Charge'
			and row.total and row.order_id
		):
			return False
		if float(row.total) >= 0:
			return False
		return self._set_expense_account(row, 'order_cancellation_account', account_map, valid_account_fields)

	def _fallback_ready_to_process(self, row):
		if row.transaction_type in ['Amazon Easy Ship Charges', 'Fulfillment Fee Refund'] \
			and not row.ready_to_process and not row.ignore_transaction:
			row.ready_to_process = 1
			return True
		return False

	@frappe.whitelist()
	def create_journal_entry(self):
		'''
		Method to create Journal Entry against payment details table row.
		Creates a separate JV for each company found in payment_details.
		'''
		# Group payment_details rows by company
		company_rows = {}
		for row in self.payment_details:
			if row.ready_to_process and row.company and row.total:
				company = row.get("company")
				if company not in company_rows:
					company_rows[company] = []
				company_rows[company].append(row)

		created_jvs = []
		journal_entry_series = frappe.db.get_single_value("eSeller Settings", "journal_entry_series")
		if company_rows and not journal_entry_series:
			frappe.throw(_("Please set Journal Entry Series in eSeller Settings"))

		for company, rows in company_rows.items():
			jv_doc = frappe.new_doc('Journal Entry')
			jv_doc.voucher_type = 'Journal Entry'
			jv_doc.company = company
			jv_doc.posting_date = self.posting_date
			jv_doc.cheque_date = self.posting_date
			jv_doc.cheque_no = self.name
			jv_doc.title = "{0} - {1}".format(self.name, company)
			jv_doc.naming_series = journal_entry_series

			total_debit = 0
			total_credit = 0

			for row in rows:
				if row.ready_to_process and row.total:
					jv_row = jv_doc.append('accounts')

					if row.transaction_type in ["Previous statement's unavailable balance", "Unavailable balance"]:
						jv_row.account = get_account_based_on_company(company, 'amazon_reserve_fund_account')
						jv_row.user_remark = row.product_details

					if row.order_id:
						jv_row.amazon_order_id = row.order_id

					if row.customer:
						jv_row.party_type = 'Customer'
						jv_row.party = row.customer
						jv_row.account = row.default_receivable_account or self.default_receivable_account

					sales_invoice_ref = False
					if row.sales_invoice:
						sales_invoice_ref = row.sales_invoice
						if frappe.db.get_value('Sales Invoice', sales_invoice_ref, 'debit_to'):
							jv_row.account = frappe.db.get_value('Sales Invoice', sales_invoice_ref, 'debit_to')

					if row.return_sales_invoice:
						sales_invoice_ref = row.return_sales_invoice

					if sales_invoice_ref:
						jv_row.reference_type = 'Sales Invoice'
						outstanding_amount = float(frappe.db.get_value('Sales Invoice', sales_invoice_ref, 'outstanding_amount'))
						if outstanding_amount >= float(row.total):
							jv_row.reference_name = sales_invoice_ref
						else:
							jv_row.user_remark = 'Invoice outstanding_amount = {0}'.format(outstanding_amount)

					if row.journal_entry:
						jv_row.reference_type = ''
						jv_row.reference_name = ''

					if row.amazon_expense_account:
						jv_row.user_remark = row.amazon_service_type if row.amazon_service_type else row.product_details
						jv_row.account = row.amazon_expense_account

					if float(row.total) > 0:
						jv_row.credit = abs(float(row.total))
						jv_row.credit_in_account_currency = abs(float(row.total))
						total_credit += abs(float(row.total))
					else:
						jv_row.debit = abs(float(row.total))
						jv_row.debit_in_account_currency = abs(float(row.total))
						total_debit += abs(float(row.total))

					if row.transaction_type not in ['Amazon Easy Ship Charges', 'Fulfillment Fee Refund']:
						if not jv_row.get("account", None) and not jv_row.get("party", None):
							frappe.throw("Please re-fetch due to missing account or party in row # {0}".format(row.idx))

					# Handling shipping charges seperately
					if row.transaction_type == 'Amazon Easy Ship Charges':
						shiping_charges_account = get_account_based_on_company(company, 'shipping_charges_account')
						if not shiping_charges_account:
							frappe.throw("Please set Amazon Shipping Charges Account for company {0} in eSeller Settings".format(company))
						jv_row.account = shiping_charges_account
						jv_row.user_remark = 'Amazon Easy Ship Charges for Order ID: {0}'.format(row.order_id)

					# Handling fulfilment fee refund seperately
					if row.transaction_type == 'Fulfillment Fee Refund':
						fulfilment_fee_refund_account = get_account_based_on_company(company, 'fulfilment_fee_refund_account')
						if not fulfilment_fee_refund_account:
							frappe.throw("Please set Fulfillment Fee Refund Account for company {0} in eSeller Settings".format(company))
						jv_row.account = fulfilment_fee_refund_account
						jv_row.user_remark = 'Fulfillment Fee Refund for Order ID: {0}'.format(row.order_id)

					if row.transaction_type in ['Amazon Easy Ship Charges', 'Fulfillment Fee Refund']:
						jv_row.reference_type = ''
						jv_row.reference_name = ''
						jv_row.party_type = ''
						jv_row.party = ''

				elif frappe.db.get_single_value("eSeller Settings", "use_reserve_lines_in_amazon_payment_entry"):
					reserve_jv_row = jv_doc.append('accounts')
					reserve_jv_row.user_remark = row.product_details

					if float(row.total) > 0:
						amazon_reserve_income_account = get_account_based_on_company(company, 'amazon_reserve_income_account')
						if not amazon_reserve_income_account:
							frappe.throw("Please set Amazon Reserve Income Account for company {0} in eSeller Settings".format(company))
						reserve_jv_row.account = amazon_reserve_income_account
						reserve_jv_row.credit = abs(float(row.total))
						reserve_jv_row.credit_in_account_currency = abs(float(row.total))
						total_credit += abs(float(row.total))
					else:
						amazon_reserve_expense_account = get_account_based_on_company(company, 'amazon_reserve_expense_account')
						if not amazon_reserve_expense_account:
							frappe.throw("Please set Amazon Reserve Expense Account for company {0} in eSeller Settings".format(company))
						reserve_jv_row.account = amazon_reserve_expense_account
						reserve_jv_row.debit = abs(float(row.total))
						reserve_jv_row.debit_in_account_currency = abs(float(row.total))
						total_debit += abs(float(row.total))

					if row.order_id:
						reserve_jv_row.amazon_order_id = row.order_id
					if row.customer:
						reserve_jv_row.party_type = 'Customer'
						reserve_jv_row.party = row.customer

			# Add balancing payment account row
			difference_amount = total_debit - total_credit
			if difference_amount:
				payment_account = get_account_based_on_company(company, 'mop_account')
				if not payment_account:
					# Getting default Bank Account
					payment_account = frappe.db.get_value('Company', company, 'default_bank_account')
				if not payment_account:
					# Getting default Cash account
					payment_account = frappe.db.get_value('Company', company, 'default_cash_account')
				if payment_account:
					jv_row = jv_doc.append('accounts')
					jv_row.account = payment_account
					if difference_amount > 0:
						jv_row.credit = difference_amount
						jv_row.credit_in_account_currency = difference_amount
					else:
						jv_row.debit = abs(difference_amount)
						jv_row.debit_in_account_currency = abs(difference_amount)
			if total_debit or total_credit:
				jv_doc.flags.ignore_mandatory = True
				jv_doc.save(ignore_permissions=True)
				jv_doc.submit()
				created_jvs.append(jv_doc)

		# Show success message with links to all created JVs
		links = ", ".join([
			'<a href="{0}">{1}</a>'.format(get_url_to_form(jv.doctype, jv.name), jv.name)
			for jv in created_jvs
		])
		frappe.msgprint(
			'Journal Entries Created: {0}'.format(links),
			alert=True,
			indicator='green'
		)

	@frappe.whitelist()
	def submit_invoices_in_background(self):
		'''
			Method to trigger Sales Invoice Submission RQ Job
		'''
		frappe.enqueue("eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings.enq_si_submit", queue="long")
		return 1 #for client side to know that the job has been triggered

	@frappe.whitelist()
	def unset_ready_to_process(self):
		'''Method to uncheck ready to process for all the rows in the payment details table'''
		for row in self.payment_details:
			row.ready_to_process = 0
			row.has_sales_order = 0
			row.sales_invoice = ''
			row.return_sales_invoice = ''
			row.journal_entry = ''
			row.company = ''
			row.amazon_expense_account = ''
			row.customer = ''
		self.save()

	def validate_missing_invoices(self):
		'''
			Validate whether all rows are processed before submission.
		'''
		remaining_count = sum(
			1 for row in self.payment_details if not (row.ready_to_process or row.ignore_transaction)
		)

		if remaining_count:
			frappe.throw(
				title="Action Required",
				msg=_("{0} row(s) are still pending invoice fetch. Please process all rows before submitting.").format(remaining_count)
			)

def get_invoice_details(amazon_order_id, is_return=0, amount=0):
	'''
		This method will return the Invoice ID and Customer
	'''
	invoice_details = {}
	si = frappe.db.exists('Sales Invoice', {'amazon_order_id':amazon_order_id, 'is_return':is_return, 'docstatus':1})
	si_list = frappe.db.get_all('Sales Invoice', {'amazon_order_id':amazon_order_id, 'is_return':is_return, 'docstatus':1})
	if len(si_list)>1:
		new_si_list = frappe.db.get_all('Sales Invoice', {'amazon_order_id':amazon_order_id, 'is_return':is_return, 'docstatus':1, 'grand_total': amount})
		si = False
		if len(new_si_list)==1:
			si = new_si_list[0].name
	if si:
		customer, company, debit_to = frappe.db.get_value('Sales Invoice', si, ['customer', 'company', 'debit_to'])
		invoice_details['sales_invoice'] = si
		invoice_details['customer'] = customer
		invoice_details['company'] = company
		invoice_details['debit_to'] = debit_to
	return invoice_details

def get_account_based_on_company(company, account_type):
	"""
		Returns the account configured for the given company and account_type
		from Amazon Payment Account, if the field exists.
	"""
	meta = frappe.get_meta("Amazon Payment Account")

	if not meta.has_field(account_type):
		return None

	return frappe.db.get_value(
		"Amazon Payment Account",
		{
			"company": company,
			"parent": "eSeller Settings",
		},
		account_type,
	)

def prefetch_invoice_details(order_ids, is_return=0):
	'''
	Bulk version of get_invoice_details. Fetches ALL matching Sales Invoices in one query,
	grouped by amazon_order_id. Ambiguous cases (multiple SI per order) are resolved
	later, in memory, using each row's own amount -- no extra query needed since
	grand_total is already fetched here.

	Returns: {order_id: [si_row, si_row, ...]}
	'''
	if not order_ids:
		return {}
	si_list = frappe.db.get_all(
		'Sales Invoice',
		filters={'amazon_order_id': ['in', order_ids], 'is_return': is_return, 'docstatus': 1},
		fields=['name', 'amazon_order_id', 'customer', 'company', 'debit_to', 'grand_total']
	)
	grouped = {}
	for si in si_list:
		grouped.setdefault(si.amazon_order_id, []).append(si)
	return grouped


def resolve_invoice_from_candidates(candidates, amount):
	'''
	In-memory equivalent of the old "requery with grand_total filter" branch.
	candidates: list of si rows for one order_id (from prefetch_invoice_details)
	'''
	if not candidates:
		return {}

	si = None
	if len(candidates) == 1:
		si = candidates[0]
	else:
		matches = [r for r in candidates if flt(r.grand_total) == flt(amount)]
		if len(matches) == 1:
			si = matches[0]

	if not si:
		return {}

	return {
		'sales_invoice': si.name,
		'customer': si.customer,
		'company': si.company,
		'debit_to': si.debit_to,
	}


def prefetch_service_types(service_type_names):
	'''Bulk version of get_amazon_service_type_details. Returns {name: expense_account}'''
	if not service_type_names:
		return {}
	rows = frappe.db.get_all(
		'Amazon Service Type',
		filters={'name': ['in', list(service_type_names)]},
		fields=['name', 'expense_account']
	)
	return {r.name: r.expense_account for r in rows}


def prefetch_replaced_jvs(order_ids):
	'''
	Bulk version of get_replaced_jv. Returns {order_id: journal_entry_name}.
	NOTE: original get_value had no order_by, so its "first match" was whatever
	Frappe's default ordering returned (typically modified desc). This preserves
	that by explicitly ordering modified desc and keeping the first hit per group.
	If your intent was actually "any match", this is equivalent or safer.
	'''
	if not order_ids:
		return {}
	rows = frappe.db.get_all(
		'Journal Entry',
		filters={'docstatus': 1, 'amazon_order_id': ['in', order_ids]},
		fields=['name', 'amazon_order_id'],
		order_by='modified desc'
	)
	result = {}
	for r in rows:
		result.setdefault(r.amazon_order_id, r.name)  # first hit per order_id wins (latest, due to ordering)
	return result


def prefetch_payment_accounts(account_types):
	'''
	Bulk version of get_account_based_on_company. Single query for ALL companies
	instead of one query per (company, account_type) pair.
	Returns: (map, valid_fields) where map = {company: {field: value, ...}}
	'''
	meta = frappe.get_meta("Amazon Payment Account")
	valid_fields = [f for f in account_types if meta.has_field(f)]
	if not valid_fields:
		return {}, valid_fields

	rows = frappe.db.get_all(
		'Amazon Payment Account',
		filters={'parent': 'eSeller Settings'},
		fields=['company'] + valid_fields
	)
	return {r.company: r for r in rows}, valid_fields
