import frappe
import csv
import xlrd
import os
from frappe.model.document import Document
from datetime import datetime

class AmazonPaymentExtractor(Document):
    def validate(self):
        self.process_payment_extractor()

    def process_payment_extractor(self):
        if self.attach:
            attached_file = frappe.get_doc("File", {"file_url": self.attach})
            file_path = frappe.get_site_path("private", "files", attached_file.file_name)
            frappe.logger().debug(f"Processing file at path: {file_path}")
            if not os.path.exists(file_path):
                frappe.throw(f"File not found at path: {file_path}")
            if attached_file.file_url.endswith(".csv"):
                self.process_csv(file_path)
            elif attached_file.file_url.endswith((".xls", ".xlsx")):
                self.process_excel(file_path)
            else:
                frappe.throw("Unsupported file format. Only CSV and Excel files are supported.")

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
            'Total product charges': "total_product_charge",
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
            self.append("payment_details", payment_detail)

@frappe.whitelist()
def create_sales_invoices(docname):
    amazon_payment_extractor = frappe.get_doc("Amazon Payment Extractor", docname)
    sales_invoice_exist = frappe.db.exists(
        "Sales Invoice", {"amazon_payment_extractor": amazon_payment_extractor.name}
    )
    customer = frappe.get_single("eSeller Settings").get("default_amazon_customer")

    if not sales_invoice_exist:
        sales_invoice_count = 0

        for payment in amazon_payment_extractor.payment_details:
            if payment.transaction_type == "Order Payment":
                new_sales_invoice = frappe.new_doc("Sales Invoice")
                new_sales_invoice.amazon_payment_extractor = amazon_payment_extractor.name
                new_sales_invoice.posting_date = payment.date
                new_sales_invoice.due_date = payment.date
                new_sales_invoice.custom_amazon_order_id = payment.order_id
                new_sales_invoice.custom_transaction_type = payment.transaction_type
                new_sales_invoice.customer = customer
                new_sales_invoice.append("items", {
                    "item_name": payment.product_details,
                    "item_code": payment.product_details,
                    "qty": 1,
                    "rate": payment.total_inr,
                    "amount": payment.total_inr,
                    "total_product_charges": payment.total_product_charges,
                    "total_promotional_rebates": payment.total_promotional_rebates,
                    "amazon_fees": payment.amazon_fees,
                    "other": payment.other,
                    "total_inr": payment.total_inr
                })

                new_sales_invoice.flags.ignore_mandatory = True
                new_sales_invoice.flags.ignore_validate = True
                new_sales_invoice.set_missing_values()

                new_sales_invoice.calculate_taxes_and_totals()

                if new_sales_invoice.net_total is None:
                    new_sales_invoice.net_total = sum(item.amount for item in new_sales_invoice.items)

                new_sales_invoice.insert(ignore_permissions=True)

                new_sales_invoice.save()
                sales_invoice_count += 1
                payment.sales_invoice_created = 1

        amazon_payment_extractor.sales_invoice_created = 1
        amazon_payment_extractor.save()
        frappe.db.commit()
        frappe.msgprint(
            f"{sales_invoice_count} Sales Invoices Created.",
            indicator="green",
            alert=True,
        )
    else:
        frappe.throw(_("Sales Invoice already exists for {0}").format(amazon_payment_extractor.name))


@frappe.whitelist()
def update_sales_invoices(docname):
    amazon_payment_extractor = frappe.get_doc("Amazon Payment Extractor", docname)
    sales_invoice_exist = frappe.db.exists(
        "Sales Invoice", {"amazon_payment_extractor": amazon_payment_extractor.name}
    )

    for payment in amazon_payment_extractor.payment_details:
        if payment.transaction_type == "Amazon Easy Ship Charges":
            sales_invoices = frappe.get_all(
                'Sales Invoice',
                filters={"custom_amazon_order_id": payment.order_id},
                fields=['name']
            )
            if sales_invoices:
                for invoice in sales_invoices:
                    existing_sales_invoice = frappe.get_doc("Sales Invoice", invoice.name)
                    existing_sales_invoice.custom_transaction_type = payment.transaction_type
                    existing_sales_invoice.append("items", {
                        "item_name": payment.product_details,
                        "item_code": payment.product_details,
                        "qty": 1,
                        "rate": payment.total_inr,
                        "amount": payment.total_inr,
                        "total_product_charges": payment.total_product_charges,
                        "total_promotional_rebates": payment.total_promotional_rebates,
                        "amazon_fees": payment.amazon_fees,
                        "other": payment.other,
                        "total_inr": payment.total_inr
                    })

                    existing_sales_invoice.flags.ignore_mandatory = True
                    existing_sales_invoice.flags.ignore_validate = True
                    existing_sales_invoice.set_missing_values()
                    existing_sales_invoice.calculate_taxes_and_totals()

                    if existing_sales_invoice.net_total is None:
                        existing_sales_invoice.net_total = sum(item.amount for item in existing_sales_invoice.items)
                    existing_sales_invoice.save()

        amazon_payment_extractor.amazon_easy_shipping_charge = 1
        amazon_payment_extractor.save()
        frappe.db.commit()
    frappe.msgprint("Sales Invoices Updated.",indicator="green",alert=True,)

@frappe.whitelist()
def update_sales_invoices_refund(docname):
    amazon_payment_extractor = frappe.get_doc("Amazon Payment Extractor", docname)
    sales_invoice_exist = frappe.db.exists(
        "Sales Invoice", {"amazon_payment_extractor": amazon_payment_extractor.name}
    )

    for payment in amazon_payment_extractor.payment_details:
        if payment.transaction_type == "Refund":
            sales_invoices = frappe.get_all(
                'Sales Invoice',
                filters={"custom_amazon_order_id": payment.order_id},
                fields=['name']
            )
            if sales_invoices:
                for invoice in sales_invoices:
                    existing_sales_invoice = frappe.get_doc("Sales Invoice", invoice.name)
                    existing_sales_invoice.custom_transaction_type = payment.transaction_type
                    existing_sales_invoice.append("items", {
                        "item_name": payment.product_details,
                        "item_code": payment.product_details,
                        "qty": 1,
                        "rate": payment.total_inr,
                        "amount": payment.total_inr,
                        "total_product_charges": payment.total_product_charges,
                        "total_promotional_rebates": payment.total_promotional_rebates,
                        "amazon_fees": payment.amazon_fees,
                        "other": payment.other,
                        "total_inr": payment.total_inr
                    })

                    existing_sales_invoice.flags.ignore_mandatory = True
                    existing_sales_invoice.flags.ignore_validate = True
                    existing_sales_invoice.set_missing_values()
                    existing_sales_invoice.calculate_taxes_and_totals()

                    if existing_sales_invoice.net_total is None:
                        existing_sales_invoice.net_total = sum(item.amount for item in existing_sales_invoice.items)
                    existing_sales_invoice.save()

        amazon_payment_extractor.refund = 1
        amazon_payment_extractor.save()
        frappe.db.commit()
    frappe.msgprint("Sales Invoices Updated.",indicator="green",alert=True,)

@frappe.whitelist()
def update_sales_invoices_fulfillment_fee_refund(docname):
    amazon_payment_extractor = frappe.get_doc("Amazon Payment Extractor", docname)
    sales_invoice_exist = frappe.db.exists(
        "Sales Invoice", {"amazon_payment_extractor": amazon_payment_extractor.name}
    )

    for payment in amazon_payment_extractor.payment_details:
        if payment.transaction_type == "Fulfillment Fee Refund":
            sales_invoices = frappe.get_all(
                'Sales Invoice',
                filters={"custom_amazon_order_id": payment.order_id},
                fields=['name']
            )
            if sales_invoices:
                for invoice in sales_invoices:
                    existing_sales_invoice = frappe.get_doc("Sales Invoice", invoice.name)
                    existing_sales_invoice.custom_transaction_type = payment.transaction_type
                    existing_sales_invoice.append("items", {
                        "item_name": payment.product_details,
                        "item_code": payment.product_details,
                        "qty": 1,
                        "rate": payment.total_inr,
                        "amount": payment.total_inr,
                        "total_product_charges": payment.total_product_charges,
                        "total_promotional_rebates": payment.total_promotional_rebates,
                        "amazon_fees": payment.amazon_fees,
                        "other": payment.other,
                        "total_inr": payment.total_inr
                    })

                    existing_sales_invoice.flags.ignore_mandatory = True
                    existing_sales_invoice.flags.ignore_validate = True
                    existing_sales_invoice.set_missing_values()
                    existing_sales_invoice.calculate_taxes_and_totals()

                    if existing_sales_invoice.net_total is None:
                        existing_sales_invoice.net_total = sum(item.amount for item in existing_sales_invoice.items)
                    existing_sales_invoice.save()

        amazon_payment_extractor.fulfillment_fee_refund = 1
        amazon_payment_extractor.save()
        frappe.db.commit()
    frappe.msgprint("Sales Invoices Updated.",indicator="green",alert=True,)
