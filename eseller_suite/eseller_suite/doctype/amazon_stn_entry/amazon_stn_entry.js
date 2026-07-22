// Copyright (c) 2026, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("Amazon STN Entry", {
	refresh(frm) {
		show_stn_processing_progress(frm);
		if (!frm.is_new()) {
			set_introduction_texts(frm);
			set_error_messages_html(frm);
			hanlde_retry_btn(frm);
		}
	},
	stn_file(frm) {
		if (!frm.doc.stn_file) {
			frm.clear_table("stn_entries");
			frm.refresh_field("stn_entries");
		}
	},
	summarise_and_download(frm) {
		handle_download_exception(frm);
	}
});

function set_introduction_texts(frm) {
	if (frm.doc.docstatus === 0) {
		frm.set_intro('');
		if (frm.doc.ready_to_process) {
			frm.set_intro('STN Entry is ready to submit', 'green');
		}
		else {
			frm.set_intro(
				__('Please <a class="jump-exceptions" style="cursor:pointer;">check the exceptions</a> before submitting.'),
				'red'
			);

			frm.page.wrapper.on('click', '.jump-exceptions', function () {
				frm.scroll_to_field('error_messages_html');
			});
		}
	}
}

function set_error_messages_html(frm) {
	frm.call('get_error_message_html').then(r => {
		if (r.message) {
			frm.set_df_property('section_break_ijoc', 'hidden', 0);
			var data = r.message;
			$(frm.fields_dict['error_messages_html'].wrapper).html(data);
			frm.refresh_fields();
			frm.set_df_property('summarise_and_download', 'hidden', 0);
		}
		else {
			frm.set_df_property('section_break_ijoc', 'hidden', 1);
			$(frm.fields_dict['error_messages_html'].wrapper).html('');
			frm.refresh_fields();
		}
	});
}

function hanlde_retry_btn(frm) {
	if (frm.doc.docstatus === 0) {
		frm.add_custom_button('Re-try Fetching', () => {
			frm.remove_custom_button('Re-try Fetching');
			frm.call('retry_fetching_data').then(r => {
				if (r.message) {
					frm.reload_doc();
				}
			})
		})
	}
}

function handle_download_exception(frm) {
	open_url_post(frappe.request.url, {
		cmd: "eseller_suite.eseller_suite.doctype.amazon_stn_entry.amazon_stn_entry.export_stock_exceptions",  // adjust to your module path
		stn_entry_name: frm.doc.name
	});
}

function show_stn_processing_progress(frm){
	// Avoid attaching multiple listeners on repeated refreshes
	frappe.realtime.off("stn_processing_progress");
	frappe.realtime.on("stn_processing_progress", (data) => {
		if (data.docname !== frm.doc.name) return; // ignore other docs' events

		frm.dashboard.show_progress(
			"Processing STN Entries",
			(data.processed / data.total) * 100,
			`${data.processed} of ${data.total} processed${data.failed ? ` (${data.failed} failed)` : ""}`
		);

		if (data.processed >= data.total) {
			frm.dashboard.hide_progress();
			frappe.show_alert({ message: "STN processing completed", indicator: "green" });
			frm.reload_doc();
		}
	});
}
