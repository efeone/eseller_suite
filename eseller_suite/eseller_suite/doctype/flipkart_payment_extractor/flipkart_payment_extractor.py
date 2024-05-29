# Copyright (c) 2024, efeone and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.csvutils import get_csv_content_from_google_sheets, read_csv_content
from eseller_suite.eseller_suite.excel_utils import (
read_xls_file_from_attached_file,
read_xlsx_file_from_attached_file,
)

ads_sheet_key_order = ['neft_id', 'payment_date', 'settlement_value', 'type', 'transaction_id', 'wallet_redeem', 'wallet_redeem_reversal', 'wallet_topup', 'wallet_refund', 'gst_on_ads_fees']


class FlipkartPaymentExtractor(Document):
	def validate(self):
		self.process_payment_extractor()

	def process_payment_extractor(self):
		if self.import_file:
			self.file_doc = frappe.get_doc("File", {"file_url": self.import_file})
			self.raw_data, self.file_extension = self.get_data_from_template_file()
			if self.raw_data and self.file_extension:
				if self.file_extension == 'xlsx':
					ads_data = self.raw_data.get('Ads')
					if ads_data:
						for row in ads_data[2:]:
							idx = 0
							ads_details_row_data = {}
							for value in row:
								if value != None:
									ads_details_key = ads_sheet_key_order[idx]
									ads_details_row_data[ads_details_key] = value
									idx += 1
							self.append('flipkart_ads_details', ads_details_row_data)


				if self.file_extension == 'csv':
					for row in self.raw_data:
						print(row)

	def get_data_from_template_file(self):
		content = None
		extension = None

		if self.file_doc:
			parts = self.file_doc.get_extension()
			extension = parts[1]
			content = self.file_doc.get_content()
			extension = extension.lstrip(".")

		elif self.file_path:
			content, extension = self.read_file(self.file_path)

		if not content:
			frappe.throw(_("Invalid or corrupted content for import"))

		if not extension:
			extension = "csv"

		if content:
			return self.read_content(content, extension)

	def read_content(self, content, extension):
		error_title = _("Template Error")
		if extension not in ("csv", "xlsx", "xls"):
			frappe.throw(_("Import template should be of type .csv, .xlsx or .xls"), title=error_title)

		if extension == "csv":
			data = read_csv_content(content)
		elif extension == "xlsx":
			data = read_xlsx_file_from_attached_file(fcontent=content)
		elif extension == "xls":
			data = read_xls_file_from_attached_file(content)

		return data, extension
