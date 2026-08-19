# Copyright (c) 2026, efeone and contributors
# For license information, please see license.txt

import frappe
from frappe import _


def execute(filters=None):
	filters = filters or {}

	columns = get_columns()
	data = get_data(filters)

	return columns, data


def get_columns():
	return [
		{
			"label": _("Sales Order"),
			"fieldname": "sales_order",
			"fieldtype": "Link",
			"options": "Sales Order",
			"width": 180
		},
		{
			"label": _("Transaction Date"),
			"fieldname": "transaction_date",
			"fieldtype": "Date",
			"width": 120
		},
		{
			"label": _("Customer"),
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			"width": 180
		},
		{
			"label": _("Amazon Order ID"),
			"fieldname": "amazon_order_id",
			"fieldtype": "Data",
			"width": 180
		},
		{
			"label": _("Amazon Payment Entry"),
			"fieldname": "amazon_payment_entry",
			"fieldtype": "Data",
			"width": 400
		}
	]


def get_data(filters):
	'''
		Fetch draft Sales Orders pending submission, filtered either by a
		transaction date range (Date Based) or a specific Amazon Payment
		Entry (Document Based), depending on the selected search mode.
	'''
	search_based_on = filters.get("search_based_on")

	conditions = ""
	values = {}

	if search_based_on == "Document Based":
		amazon_payment_entry = filters.get("amazon_payment_entry")
		if not amazon_payment_entry:
			frappe.throw(_("Please select an Amazon Payment Entry."))
		conditions += " AND ape.name = %(amazon_payment_entry)s"
		values["amazon_payment_entry"] = amazon_payment_entry
	else:
		from_date = filters.get("from_date")
		to_date = filters.get("to_date")
		if not (from_date and to_date):
			frappe.throw(_("Please select both From Date and To Date."))
		conditions += " AND so.transaction_date BETWEEN %(from_date)s AND %(to_date)s"
		values["from_date"] = from_date
		values["to_date"] = to_date

	return frappe.db.sql("""
		SELECT
			so.name AS sales_order,
			so.transaction_date AS transaction_date,
			so.customer AS customer,
			so.amazon_order_id AS amazon_order_id,
			GROUP_CONCAT(DISTINCT ape.name ORDER BY ape.name SEPARATOR ', ') AS amazon_payment_entry
		FROM
			`tabSales Order` so
		LEFT JOIN
			`tabAmazon Payment Entry Item` apei
				ON apei.order_id = so.amazon_order_id
		LEFT JOIN
			`tabAmazon Payment Entry` ape
				ON ape.name = apei.parent
				AND ape.docstatus < 2
		WHERE
			so.docstatus = 0
			AND IFNULL(so.amazon_order_id, '') != ''
			{conditions}
			AND EXISTS (
				SELECT 1
				FROM `tabAFN Order Shipment Log` afn
				WHERE afn.amazon_order_id = so.amazon_order_id
			)
		GROUP BY
			so.name
		ORDER BY
			so.transaction_date ASC,
			so.creation ASC
	""".format(conditions=conditions), values, as_dict=True)
