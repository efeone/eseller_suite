// Copyright (c) 2026, efeone and contributors
// For license information, please see license.txt

frappe.query_reports["Submission Pending Sales Orders"] = {
    "filters": [
        {
            "fieldname": "search_based_on",
            "label": __("Search Based On"),
            "fieldtype": "Select",
            "options": "Date Based\nDocument Based",
            "default": "Date Based",
            "reqd": 1,
            "on_change": function () {
                toggle_search_filters();
                frappe.query_report.refresh();
            }
        },
        {
            "fieldname": "from_date",
            "label": __("From Date"),
            "fieldtype": "Date",
            "default": frappe.datetime.add_days(frappe.datetime.get_today(), -30)
        },
        {
            "fieldname": "to_date",
            "label": __("To Date"),
            "fieldtype": "Date",
            "default": frappe.datetime.get_today()
        },
        {
            "fieldname": "amazon_payment_entry",
            "label": __("Amazon Payment Entry"),
            "fieldtype": "Link",
            "options": "Amazon Payment Entry",
            "get_query": function () {
                return {
                    "filters": {
                        "docstatus": 0
                    }
                };
            }
        }
    ],
    onload: function () {
        toggle_search_filters();
    }
};

function toggle_search_filters() {
    let is_date_based = frappe.query_report.get_filter_value("search_based_on") === "Date Based";

    ["from_date", "to_date"].forEach(function (fieldname) {
        let filter = frappe.query_report.get_filter(fieldname);
        filter.df.reqd = is_date_based ? 1 : 0;
        filter.toggle(is_date_based);
        filter.refresh();
    });

    let payment_entry = frappe.query_report.get_filter("amazon_payment_entry");
    payment_entry.df.reqd = is_date_based ? 0 : 1;
    payment_entry.toggle(!is_date_based);
    payment_entry.refresh();
}
