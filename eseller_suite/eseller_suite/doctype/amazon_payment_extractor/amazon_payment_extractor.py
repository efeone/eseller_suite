import frappe
import csv
import xlrd
import os
from frappe.model.document import Document
from datetime import datetime
from frappe.utils import get_link_to_form

ignored_transaction_types = ["Unavailable balance", "Previous statement's unavailable balance"]

class AmazonPaymentExtractor(Document):
    def validate(self):
        self.process_payment_extractor()
        if self.payment_details:
            missing_items = ''
            for row in self.payment_details:
                if frappe.db.exists('Item', { 'custom_amazon_item_code': row.product_details }):
                    row.item_code = frappe.db.get_value('Item', { 'custom_amazon_item_code': row.product_details })
                else:
                    missing_row = 'Row : {0}, Product Details: {1}\n'.format(row.idx, row.product_details)
                    missing_items += missing_row
            self.missing_items = missing_items

    def process_payment_extractor(self):
        if self.attach:
            attached_file = frappe.get_doc("File", {"file_url": self.attach})
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
            self.missing_items = ''


    def process_csv(self, file_path):
        with open(file_path, "r") as file:
            csv_reader = csv.DictReader(file)
            for row in csv_reader:
                self.save_payment_details(row)

    def process_excel(self, file_path):
        workbook = xlrd.open_workbook(file_path)
        sheet = workbook.sheet_by_index(0)
        headers = sheet.row_values(0)
        for row_idx in range(1, sheet.nrows):
            row = dict(zip(headers, sheet.row_values(row_idx)))
            self.save_payment_details(row)

    def parse_date(self, date_str):
        try:
            parsed_date = datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
            return parsed_date
        except ValueError:
            frappe.throw(f"Invalid date format: {date_str}")

    def save_payment_details(self, row):
        key_mapping = {
            '\ufeff"Date"': "date",
            'Transaction type': "transaction_type",
            'Order ID': "order_id",
            'Product Details': "product_details",
            'Total product charges': "total_product_charges",
            'Total promotional rebates': "total_promotional_rebates",
            'Amazon fees': "amazon_fees",
            'Other': "other",
            'Total (INR)': "total_inr"
        }
        payment_detail = {}
        for csv_key, internal_key in key_mapping.items():
            value = row.get(csv_key)
            if internal_key == "date":
                value = self.parse_date(value)
            payment_detail[internal_key] = value

        if not any(
            pd.transaction_type == payment_detail["transaction_type"] and
            pd.order_id == payment_detail["order_id"] and
            pd.product_details == payment_detail["product_details"]
            for pd in self.payment_details
        ):
            if payment_detail.get('transaction_type'):
                if payment_detail.get('transaction_type') not in ignored_transaction_types:
                    self.append("payment_details", payment_detail)

@frappe.whitelist()
def create_sales_invoices(docname):
    '''
        Method to create Sales Inovoices
    '''
    amazon_payment_extractor = frappe.get_doc('Amazon Payment Extractor', docname)
    default_amazon_customer = frappe.db.get_single_value('eSeller Settings', 'default_amazon_customer')
    default_pos_profile = frappe.db.get_single_value('eSeller Settings', 'default_pos_profile')
    if not default_amazon_customer:
        frappe.throw('Please configure the `Default Amazon Customer` in {0}'.format(get_link_to_form('eSeller Settings', 'eSeller Settings')))
    if not default_pos_profile:
        frappe.throw('Please configure the `Default POS Profile` in {0}'.format(get_link_to_form('eSeller Settings', 'eSeller Settings')))
    sales_invoice_count = 0
    for payment in amazon_payment_extractor.payment_details:
        if payment.transaction_type == 'Order Payment' and payment.item_code:
            new_sales_invoice = frappe.new_doc('Sales Invoice')
            new_sales_invoice.posting_date = payment.date
            new_sales_invoice.custom_amazon_order_id = payment.order_id
            new_sales_invoice.custom_transaction_type = payment.transaction_type
            new_sales_invoice.customer = default_amazon_customer
            new_sales_invoice.update_stock = 1
            new_sales_invoice.is_pos = 1
            new_sales_invoice.pos_profile = default_pos_profile
            new_sales_invoice.append('items', {
                'item_code': payment.item_code,
                'qty': 1,
                'rate': payment.total_inr,
                'amount': payment.total_inr,
                'custom_total_product_charges': payment.total_product_charges,
                'custom_total_promotional_rebates': payment.total_promotional_rebates,
                'custom_amazon_fees': payment.amazon_fees,
                'custom_other': payment.other,
                'custom_total_inr': payment.total_inr,
                'allow_zero_valuation_rate': 1
            })
            new_sales_invoice.flags.ignore_mandatory = True
            new_sales_invoice.flags.ignore_validate = True
            new_sales_invoice.set_missing_values()
            new_sales_invoice.calculate_taxes_and_totals()
            new_sales_invoice.net_total = sum(item.amount for item in new_sales_invoice.items)
            new_sales_invoice.outstanding_amount = 0
            new_sales_invoice.disable_rounded_total = 1
            new_sales_invoice.payments[0].amount = new_sales_invoice.net_total
            new_sales_invoice.paid_amount = new_sales_invoice.net_total
            new_sales_invoice.save()
            sales_invoice_count += 1
            payment.sales_invoice = new_sales_invoice.name
    amazon_payment_extractor.sales_invoice_created = 1
    amazon_payment_extractor.save()
    frappe.msgprint(
        f"{sales_invoice_count} Sales Invoices Created.",
        indicator="green",
        alert=True,
    )


@frappe.whitelist()
def update_sales_invoices_with_shipping_charges(docname):
    '''
        Method to add shipping charges to existing sales invoices
    '''
    amazon_payment_extractor = frappe.get_doc('Amazon Payment Extractor', docname)
    for payment in amazon_payment_extractor.payment_details:
        if payment.transaction_type == 'Amazon Easy Ship Charges' and payment.item_code:
            if frappe.db.exists('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':0 }):
                sales_invoice_id = frappe.db.get_value('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':0 })
                existing_sales_invoice = frappe.get_doc('Sales Invoice', sales_invoice_id)
                existing_sales_invoice.append('items', {
                    'item_code': payment.item_code,
                    'qty': 1,
                    'rate': payment.total_inr,
                    'amount': payment.total_inr,
                    'custom_total_product_charges': payment.total_product_charges,
                    'custom_total_promotional_rebates': payment.total_promotional_rebates,
                    'custom_amazon_fees': payment.amazon_fees,
                    'custom_other': payment.other,
                    'custom_total_inr': payment.total_inr,
                    'allow_zero_valuation_rate': 1
                })
                existing_sales_invoice.flags.ignore_mandatory = True
                existing_sales_invoice.flags.ignore_validate = True
                existing_sales_invoice.set_missing_values()
                existing_sales_invoice.calculate_taxes_and_totals()
                existing_sales_invoice.net_total = sum(item.amount for item in existing_sales_invoice.items)
                existing_sales_invoice.save()
                payment.sales_invoice = existing_sales_invoice.name
    amazon_payment_extractor.amazon_easy_shipping_charge = 1
    amazon_payment_extractor.save()
    frappe.msgprint('Sales Invoices updated with Shipping Charges', indicator='green', alert=True)

@frappe.whitelist()
def create_return_invoice(docname):
    '''
        Method to Create Return Invoices
    '''
    amazon_payment_extractor = frappe.get_doc("Amazon Payment Extractor", docname)
    for payment in amazon_payment_extractor.payment_details:
        if payment.transaction_type == 'Refund' and payment.item_code:
            if frappe.db.exists('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':0 }):
                sales_invoice_id = frappe.db.get_value('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':0 })
                existing_sales_invoice = frappe.get_doc('Sales Invoice', sales_invoice_id)
                existing_sales_invoice.flags.ignore_mandatory = True
                existing_sales_invoice.flags.ignore_validate = True
                existing_sales_invoice.submit()
            if frappe.db.exists('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':1 }):
                sales_invoice_id = frappe.db.get_value('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':1 })
                existing_sales_invoice = frappe.get_doc('Sales Invoice', sales_invoice_id)
                return_invoice = frappe.new_doc('Sales Invoice')
                return_invoice.posting_date = payment.date
                return_invoice.custom_amazon_order_id = payment.order_id
                return_invoice.customer = existing_sales_invoice.customer
                return_invoice.custom_transaction_type = payment.transaction_type
                return_invoice.is_return = 1
                return_invoice.return_against = existing_sales_invoice.name
                return_invoice.update_outstanding_for_self = 0
                return_invoice.update_stock = 1
                return_invoice.is_pos = existing_sales_invoice.is_pos
                return_invoice.pos_profile = existing_sales_invoice.pos_profile
                return_invoice.items = []
                return_invoice.append('items', {
                    'item_code': payment.item_code,
                    'qty': -1,
                    'rate': payment.total_inr*-1,
                    'amount': payment.total_inr,
                    'custom_total_product_charges': payment.total_product_charges,
                    'custom_total_promotional_rebates': payment.total_promotional_rebates,
                    'custom_amazon_fees': payment.amazon_fees,
                    'custom_other': payment.other,
                    'custom_total_inr': payment.total_inr,
                    'allow_zero_valuation_rate': 1
                })
                return_invoice.flags.ignore_mandatory = True
                return_invoice.flags.ignore_validate = True
                return_invoice.set_missing_values()
                return_invoice.calculate_taxes_and_totals()
                return_invoice.net_total = sum(item.amount for item in return_invoice.items)
                return_invoice.save()
                payment.sales_invoice = return_invoice.name
    amazon_payment_extractor.refund = 1
    amazon_payment_extractor.save()
    frappe.msgprint('Return Sales Invoices created', indicator='green', alert=True,)

@frappe.whitelist()
def update_sales_invoices_with_fulfillment_fee_refund(docname):
    '''
        Method to add Fulfillment fee refund amount to return invoices
    '''
    amazon_payment_extractor = frappe.get_doc('Amazon Payment Extractor', docname)
    for payment in amazon_payment_extractor.payment_details:
        if payment.transaction_type == 'Fulfillment Fee Refund' and payment.item_code:
            if frappe.db.exists('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':0, 'is_return':1 }):
                sales_invoice_id = frappe.db.get_value('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':0, 'is_return':1 })
                existing_sales_invoice = frappe.get_doc('Sales Invoice', sales_invoice_id)
                existing_sales_invoice.append('items', {
                    'item_code': payment.item_code,
                    'qty': 1,
                    'rate': payment.total_inr,
                    'amount': payment.total_inr,
                    'custom_total_product_charges': payment.total_product_charges,
                    'custom_total_promotional_rebates': payment.total_promotional_rebates,
                    'custom_amazon_fees': payment.amazon_fees,
                    'custom_other': payment.other,
                    'custom_total_inr': payment.total_inr,
                    'allow_zero_valuation_rate': 1
                })
                existing_sales_invoice.flags.ignore_mandatory = True
                existing_sales_invoice.flags.ignore_validate = True
                existing_sales_invoice.set_missing_values()
                existing_sales_invoice.calculate_taxes_and_totals()
                existing_sales_invoice.net_total = sum(item.amount for item in existing_sales_invoice.items)
                existing_sales_invoice.save()
                payment.sales_invoice = existing_sales_invoice.name
    amazon_payment_extractor.fulfillment_fee_refund = 1
    amazon_payment_extractor.save()
    frappe.msgprint('Sales Invoices updated with Fulfillment Fee Refund', indicator='green', alert=True)

@frappe.whitelist()
def submit_all_invoices(docname):
    '''
        Method to Submit all sales invoices which is of transaction_type Order Payment
    '''
    amazon_payment_extractor = frappe.get_doc('Amazon Payment Extractor', docname)
    for payment in amazon_payment_extractor.payment_details:
        if payment.transaction_type == 'Order Payment':
            if frappe.db.exists('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':0, 'custom_transaction_type':payment.transaction_type }):
                sales_invoice_id = frappe.db.get_value('Sales Invoice', { 'custom_amazon_order_id':payment.order_id, 'docstatus':0, 'custom_transaction_type':payment.transaction_type })
                sales_invoice = frappe.get_doc('Sales Invoice', sales_invoice_id)
                sales_invoice.flags.ignore_mandatory = True
                sales_invoice.flags.ignore_validate = True
                sales_invoice.outstanding_amount = 0
                sales_invoice.submit()
    amazon_payment_extractor.invoices_submitted = 1
    amazon_payment_extractor.save()
    frappe.msgprint('Sales Invoices submited succesfully', indicator='green', alert=True)
