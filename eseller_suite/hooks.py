app_name = "eseller_suite"
app_title = "eSeller Suite"
app_publisher = "efeone"
app_description = "A comprehensive frappe app designed to help selling partners thrive on various online platforms like Amazon and Flipkart. With powerful tools for optimization, analytics, and automation, sellers can boost sales, optimize listings, and stay ahead of the competition effortlessly."
app_email = "info@efeone.com"
app_license = "mit"
# required_apps = []

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/eseller_suite/css/eseller_suite.css"
# app_include_js = "/assets/eseller_suite/js/eseller_suite.js"

# include js, css files in header of web template
# web_include_css = "/assets/eseller_suite/css/eseller_suite.css"
# web_include_js = "/assets/eseller_suite/js/eseller_suite.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "eseller_suite/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {
	"Sales Order" : "eseller_suite/custom_script/sales_order/sales_order.js",
	"Item" : "eseller_suite/custom_script/item/item.js",
	"Stock Entry" : "eseller_suite/custom_script/stock_entry/stock_entry.js",
	"Purchase Invoice" : "eseller_suite/custom_script/purchase_invoice/purchase_invoice.js",
	"Sales Invoice" : "eseller_suite/custom_script/sales_invoice/sales_invoice.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "eseller_suite/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "eseller_suite.utils.jinja_methods",
# 	"filters": "eseller_suite.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "eseller_suite.install.before_install"
after_install = "eseller_suite.install.after_install"
after_migrate = "eseller_suite.setup.after_migrate"

# Uninstallation
# ------------

before_uninstall = "eseller_suite.setup.before_uninstall"
# after_uninstall = "eseller_suite.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "eseller_suite.utils.before_app_install"
# after_app_install = "eseller_suite.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "eseller_suite.utils.before_app_uninstall"
# after_app_uninstall = "eseller_suite.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "eseller_suite.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

override_doctype_class = {
	"Sales Order": "eseller_suite.eseller_suite.custom_script.sales_order.sales_order.SalesOrderOverride",
	"Journal Entry": "eseller_suite.eseller_suite.custom_script.journal_entry.journal_entry.JournalEntryOverride"
}

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	'Sales Invoice':{
		"before_validate": "eseller_suite.eseller_suite.custom_script.sales_invoice.sales_invoice.before_validate",
		"validate": "eseller_suite.eseller_suite.custom_script.sales_invoice.sales_invoice.validate",
		"after_insert": "eseller_suite.eseller_suite.custom_script.sales_invoice.sales_invoice.after_insert",
		"before_submit": [
            "eseller_suite.eseller_suite.custom_script.sales_invoice.sales_invoice.before_submit",
            "eseller_suite.eseller_suite.doctype.amazon_stn_entry.amazon_stn_entry.validate_amazon_invoice_total_on_submit",
        ],
		"on_submit": "eseller_suite.eseller_suite.custom_script.sales_invoice.sales_invoice.on_submit",
		"on_cancel": "eseller_suite.eseller_suite.custom_script.sales_invoice.sales_invoice.on_cancel",
		"on_trash": "eseller_suite.eseller_suite.custom_script.sales_invoice.sales_invoice.on_trash",
	},
	'Purchase Receipt':{
		"before_validate": "eseller_suite.eseller_suite.custom_script.purchase_receipt.purchase_receipt.create_barcodes",
		"before_submit": "eseller_suite.eseller_suite.custom_script.purchase_receipt.purchase_receipt.activate_barcodes",
		"on_cancel": "eseller_suite.eseller_suite.custom_script.purchase_receipt.purchase_receipt.delete_barcodes",
	},
	'Stock Entry':{
		"before_validate": "eseller_suite.eseller_suite.custom_script.purchase_receipt.purchase_receipt.create_barcodes",
		"before_submit": "eseller_suite.eseller_suite.custom_script.stock_entry.stock_entry.transfer_barcodes",
		"before_insert": "eseller_suite.eseller_suite.custom_script.stock_entry.stock_entry.before_insert_custom",
		"on_cancel": "eseller_suite.eseller_suite.custom_script.stock_entry.stock_entry.on_cancel",
		"validate": "eseller_suite.eseller_suite.custom_script.stock_entry.stock_entry.validate",
		"on_submit": "eseller_suite.eseller_suite.custom_script.stock_entry.stock_entry.on_submit",
	},
	'Purchase Invoice':{
		"on_cancel": "eseller_suite.eseller_suite.custom_script.purchase_invoice.purchase_invoice.on_cancel",
		"on_submit": "eseller_suite.eseller_suite.custom_script.purchase_invoice.purchase_invoice.on_submit",
		"before_save": "eseller_suite.eseller_suite.custom_script.purchase_invoice.purchase_invoice.before_save",
		"on_update_after_submit": "eseller_suite.eseller_suite.custom_script.purchase_invoice.purchase_invoice.on_update_after_submit",
        "before_submit": "eseller_suite.eseller_suite.doctype.amazon_stn_entry.amazon_stn_entry.validate_amazon_invoice_total_on_submit"
	},
	'Product Bundle':{
        "after_insert": "eseller_suite.eseller_suite.custom_script.product_bundle.product_bundle.mark_item_as_bundle",
		"validate": "eseller_suite.eseller_suite.custom_script.product_bundle.product_bundle.validate_product_bundle_items",
    },
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"all": [
		"eseller_suite.eseller_suite.doctype.amazon_report_api_log.amazon_report_api_log.get_report_status_scheduler"
	],
	"daily": [
		"eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings.create_daily_reports_schedule",
	],
	"daily_long": [
		"eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings.schedule_get_order_details_daily"
	],
	"hourly_long": [
		"eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_sp_api_settings.schedule_get_order_details",
	],
# 	"weekly": [
# 		"eseller_suite.tasks.weekly"
# 	],
# 	"monthly": [
# 		"eseller_suite.tasks.monthly"
# 	],
}

# Testing
# -------

# before_tests = "eseller_suite.install.before_tests"

# Overriding Methods
# ------------------------------
#
override_whitelisted_methods = {
	"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice": "eseller_suite.eseller_suite.custom_script.sales_order.sales_order.make_sales_invoice",
	"erpnext.controllers.queries.item_query": "eseller_suite.eseller_suite.queries.custom_item_query"
}

#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "eseller_suite.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["eseller_suite.utils.before_request"]
# after_request = ["eseller_suite.utils.after_request"]

# Job Events
# ----------
# before_job = ["eseller_suite.utils.before_job"]
# after_job = ["eseller_suite.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"eseller_suite.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Fixtures

fixtures = [
	{
		"dt": "Amazon State Mapping"
	},
	{
		"dt": "Amazon Report Type"
	}
]
