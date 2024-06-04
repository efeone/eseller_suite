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
order_sheet_key_order = ['order_date','dispatch_date','order_type','fulfilment_type','seller_sku','quantity','product_sub_category','additional_information','return_type','shopsy_order','item_return_status',' ','invoice_id','invoice_date',' ','total_rs']


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
					order_data = self.raw_data.get('Orders')
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

					if order_data:
						for row in order_data[3:]:
							idx = 0
							order_details_row_data = {}
							for values in row[54:70]:
								order_details_key = order_sheet_key_order[idx]
								order_details_row_data[order_details_key] = values
								idx += 1
							print(order_details_row_data)
							self.append('flipkart_order_details', order_details_row_data)

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


@frappe.whitelist()
def create_ads_purchase_invoices(docname):
    flipkart_payment_extractor = frappe.get_doc("Flipkart Payment Extractor", docname)
    purchase_invoice_exist = frappe.db.exists(
        "Sales Invoice", {"flipkart_payment_extractor": flipkart_payment_extractor.name}
    )
    supplier = frappe.get_single("eSeller Settings").get("default_flipkart_supplier")

    if not purchase_invoice_exist:
        purchase_invoice_count = 0

        for payment in flipkart_payment_extractor.flipkart_ads_details:
            if payment.type == "redeem" or payment.type == "topup" or payment.type == "refund":
                new_purchase_invoice = frappe.new_doc("Purchase Invoice")
                new_purchase_invoice.flipkart_payment_extractor = flipkart_payment_extractor.name
                new_purchase_invoice.due_date = payment.payment_date
                new_purchase_invoice.custom_transaction_id = payment.transaction_id
                new_purchase_invoice.custom_wallet_redeem = payment.wallet_redeem
                new_purchase_invoice.custom_wallet_topup = payment.wallet_topup
                new_purchase_invoice.custom_wallet_refund_ = payment.wallet_refund
                new_purchase_invoice.custom_transaction_type = payment.type
                new_purchase_invoice.supplier = supplier
                new_purchase_invoice.append("items", {
                    "item_name": payment.type,
                    "item_code": payment.type,
                    "qty": 1,
                    "rate": payment.settlement_value,
                    "amount": payment.settlement_value,
                    "custom_wallet_redeem_reversal": payment.wallet_redeem_reversal,
					"custom_settlement_value": payment.settlement_value,
                    "custom_gst_on_ads_fees": payment.gst_on_ads_fees,
                    "custom__wallet_topup_": payment.wallet_topup,
                    "custom_wallet_redeem": payment.wallet_redeem,
                    "custom__wallet_refund_": payment.wallet_refund
                })

                new_purchase_invoice.flags.ignore_mandatory = True
                new_purchase_invoice.flags.ignore_validate = True
                new_purchase_invoice.set_missing_values()

                new_purchase_invoice.calculate_taxes_and_totals()

                if new_purchase_invoice.net_total is None:
                    new_purchase_invoice.net_total = sum(item.amount for item in new_purchase_invoice.items)

                new_purchase_invoice.insert(ignore_permissions=True)

                new_purchase_invoice.submit()
                purchase_invoice_count += 1

        flipkart_payment_extractor.purchase_invoice_created = 1
        flipkart_payment_extractor.save()
        frappe.db.commit()
        frappe.msgprint(
            f"{purchase_invoice_count} Purchase Invoices Created.",
            indicator="green",
            alert=True,
        )

@frappe.whitelist()
def create_order_purchase_invoices(docname):
    flipkart_payment_extractor = frappe.get_doc("Flipkart Payment Extractor", docname)
    purchase_invoice_exist = frappe.db.exists(
        "Sales Invoice", {"flipkart_payment_extractor": flipkart_payment_extractor.name}
    )
    supplier = frappe.get_single("eSeller Settings").get("default_flipkart_supplier")

    if not purchase_invoice_exist:
        purchase_invoice_count = 0

        for order in flipkart_payment_extractor.flipkart_order_details:
            if order.order_type == "prepaid":
                new_purchase_invoice = frappe.new_doc("Purchase Invoice")
                new_purchase_invoice.flipkart_payment_extractor = flipkart_payment_extractor.name
                new_purchase_invoice.due_date = order.order_date
                new_purchase_invoice.supplier = supplier
                new_purchase_invoice.append("items", {
                    "item_name": order.seller_sku,
                    "item_code": order.seller_sku,
                    "qty": order.quantity,
                    "rate": order.total_rs,
                    "amount": order.total_rs,
                    "custom_dispatch_date": order.dispatch_date,
					"custom_order_type": order.order_type,
                    "custom_fulfilment_type": order.fulfilment_type,
                    "custom_item_return_status": order.item_return_status,
                    "custom_shopsy_order": order.shopsy_order,
                    "custom_return_type": order.return_type,
					"custom_additional_information": order.additional_information,

                })

                new_purchase_invoice.flags.ignore_mandatory = True
                new_purchase_invoice.flags.ignore_validate = True
                new_purchase_invoice.set_missing_values()

                new_purchase_invoice.calculate_taxes_and_totals()

                if new_purchase_invoice.net_total is None:
                    new_purchase_invoice.net_total = sum(item.amount for item in new_purchase_invoice.items)

                new_purchase_invoice.insert(ignore_permissions=True)

                new_purchase_invoice.submit()
                purchase_invoice_count += 1

        flipkart_payment_extractor.flipkart_order = 1
        flipkart_payment_extractor.save()
        frappe.db.commit()
        frappe.msgprint(
            f"{purchase_invoice_count} Purchase Invoices Created.",
            indicator="green",
            alert=True,
        )
