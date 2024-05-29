import frappe
from frappe.model.mapper import get_mapped_doc
from frappe.utils import flt

def before_submit(doc, method):
    if doc.custom_transaction_type == "Refund":
        doc.is_return = 1

def auto_create_return_sales_invoice(doc, method):
    if doc.custom_transaction_type == "Refund" and doc.docstatus == 1 and not doc.is_return:
        si = frappe.copy_doc(doc)
        si.is_return = 1
        si.return_against = doc.name
        si.amended_from = None
        si.name = None

        for item in si.items:
            item.qty = -item.qty

        si.insert(ignore_permissions=True)
        si.submit()

        frappe.msgprint(f"Return Sales Invoice {si.name} created automatically.", indicator="green", alert=True)
