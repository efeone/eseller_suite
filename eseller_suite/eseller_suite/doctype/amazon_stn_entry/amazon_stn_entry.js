// Copyright (c) 2026, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("Amazon STN Entry", {
	refresh(frm) {
		// Re-attach listener on every refresh (e.g. after a page reload while job is running)
		setup_stn_progress_listener(frm);
		call_and_setup_stn_progress(frm);

		if (!frm.is_new()) {
			set_introduction_texts(frm);
			hanlde_retry_btn(frm);
			if(frm.doc.docstatus===0 || frm.doc.processing_completed===1){
				set_error_messages_html(frm);
			}
		}

	},
	stn_file(frm) {
		if (!frm.doc.stn_file) {
			frm.clear_table("stn_entries");
			frm.refresh_field("stn_entries");
		}
	},
	on_submit(frm) {
		show_progress_banner(frm, __("Queuing batches..."), 0);
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

function call_and_setup_stn_progress(frm) {
	if (frm.doc.docstatus === 1 && has_pending_rows(frm)) {
		frappe.call({
			method: "eseller_suite.eseller_suite.doctype.amazon_stn_entry.amazon_stn_entry.get_stn_progress",
			args: { docname: frm.doc.name },
			callback(r) {
				const progress = r.message;

				if (!progress || progress.stage === "complete") {
					hide_progress_banner(frm);
					return;
				}

				if (is_stale(progress)) {
					show_stalled_banner(frm, progress);
					return;
				}

				handle_progress_event(frm, progress);
			},
		});
	}
}

const STALE_THRESHOLD_MS = 2 * 60 * 1000; // 2 minutes

function has_pending_rows(frm) {
	const CANCEL_TYPES = ["FC_REMOVAL-Cancel", "EInvoiceCancel"];

	return (frm.doc.stn_entries || []).some((r) => {
		if (CANCEL_TYPES.includes(r.transaction_type)) {
			return false; // cancel rows don't set transactions_created, so exclude them from the pending check
		}
		return r.ready_to_process && !r.transactions_created;
	});
}

function is_stale(progress) {
	if (!progress.timestamp) return false;
	const age = Date.now() - new Date(progress.timestamp).getTime();
	return age > STALE_THRESHOLD_MS;
}

function setup_stn_progress_listener(frm) {
	if (frm.__stn_listener_attached) return;
	frm.__stn_listener_attached = true;

	frappe.realtime.on("stn_processing_progress", (data) => {
		if (data.docname !== frm.doc.name) return;
		handle_progress_event(frm, data);
	});
}

function handle_progress_event(frm, data) {
	const { stage, batch_index, total_batches, current_row, total_rows_in_batch, current, total, message } = data;

	switch (stage) {
		case "queued": {
			show_progress_banner(frm, message, 0);
			break;
		}

		case "creating_entries": {
			const batch_fraction = total_rows_in_batch ? current_row / total_rows_in_batch : 0;
			const overall_pct = Math.round((((batch_index || 0) + batch_fraction) / (total_batches || 1)) * 85);
			show_progress_banner(frm, `${__("Batch")} ${(batch_index ?? 0) + 1}/${total_batches}: ${message}`, overall_pct);
			break;
		}

		case "row_failed": {
			frappe.show_alert({ message: __(message), indicator: "orange" }, 5);
			break;
		}

		case "batch_complete": {
			const overall_pct = Math.round((((batch_index ?? 0) + 1) / (total_batches || 1)) * 85);
			show_progress_banner(frm, message, overall_pct);
			break;
		}

		case "submitting_entries": {
			const pct = total ? 85 + Math.round((current / total) * 15) : 90;
			show_progress_banner(frm, message, pct);
			break;
		}

		case "complete": {
			show_progress_banner(frm, message, 100);
			frappe.show_alert({ message: __("STN processing complete"), indicator: "green" }, 5);
			setTimeout(() => {
				hide_progress_banner(frm);
				frm.reload_doc();
			}, 1500);
			break;
		}
	}
}

function show_stalled_banner(frm, progress) {
	if (!frm.__stn_progress_area) {
		build_progress_area(frm);
	}
	frm.__stn_progress_area.find(".stn-progress-label").html(
		`${__("Processing may have stalled")} (${__("last update")}: ${frappe.datetime.comment_when(progress.timestamp)}). ` +
		`<a href="/app/background-jobs">${__("Check Background Jobs")}</a>`
	);
	frm.__stn_progress_area.find(".stn-progress-bar")
		.css("width", "100%")
		.removeClass("progress-bar-success")
		.addClass("progress-bar-danger");
}

function build_progress_area(frm) {
	frm.__stn_progress_area = $(
		`<div class="stn-progress-wrapper" style="margin: 10px 0;">
			<div class="stn-progress-label" style="margin-bottom: 4px; font-size: 12px; color: var(--text-muted);"></div>
			<div class="progress" style="height: 8px;">
				<div class="progress-bar stn-progress-bar" role="progressbar" style="width: 0%;"></div>
			</div>
		</div>`
	).prependTo(frm.layout.wrapper || frm.$wrapper.find(".form-layout"));
}

function show_progress_banner(frm, message, percent) {
	if (!frm.__stn_progress_area) {
		build_progress_area(frm);
	}
	frm.__stn_progress_area.find(".stn-progress-label").text(message);
	frm.__stn_progress_area.find(".stn-progress-bar")
		.removeClass("progress-bar-danger")
		.css("width", `${percent}%`)
		.attr("aria-valuenow", percent);
}

function hide_progress_banner(frm) {
	if (frm.__stn_progress_area) {
		frm.__stn_progress_area.fadeOut(400, () => {
			frm.__stn_progress_area.remove();
			frm.__stn_progress_area = null;
		});
	}
}
