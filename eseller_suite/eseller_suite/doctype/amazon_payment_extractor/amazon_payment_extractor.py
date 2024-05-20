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

        self.append("payment_details", payment_detail)
