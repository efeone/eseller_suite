// Copyright (c) 2024, efeone and contributors
// For license information, please see license.txt

frappe.ui.form.on("Amazon Payment Entry", {
	setup(frm) {
		const original_dirty = frm.dirty;
		frm.dirty = function () {
			original_dirty.apply(frm, arguments);
			// runs whenever form becomes dirty
			on_form_dirty(frm);
		};
	},
	onload(frm) {
		if (frm.is_new()) {
			frappe.db.get_single_value('eSeller Settings', 'default_mode_of_payment').then(default_mode_of_payment => {
				frm.set_value('mode_of_payment', default_mode_of_payment);
			});
		}
		frm.set_df_property('payment_details', 'cannot_add_rows', true)
	},
	refresh(frm) {
		if (frm.doc.in_progress) {
			frm.set_intro('Background syncing is in progress, Please wait and reload again', 'orange');
			frm.disable_form();
			frm.disable_save();
		}
		if (!frm.is_new() && frm.doc.docstatus === 0 && frm.doc.in_progress === 0) {
			handle_custom_buttons(frm);
		}
		if (!frm.is_new() && frm.doc.docstatus === 0) {
			set_introduction_texts(frm);
		}
		handle_realtime_updates(frm);
	},
	reset_progress(frm) {
		frm.set_value('in_progress', 0);
		frm.save_or_update();
		frm.reload_doc();
	}
});

function on_form_dirty(frm) {
	if (frm.doc.docstatus === 0) {
		frm.enable_save();
	}
}

function handle_realtime_updates(frm) {
	frappe.realtime.on("fetch_invoice_details", (data) => {
		frappe.hide_msgprint(true);
		frappe.show_progress('Fetching Invoice Details...', data.progress, data.total, __("Fetching {0} of {1} invoices", [data.progress, data.total]), true);
		if (data.progress === data.total) {
			frm.reload_doc();
		}
	});
	frappe.realtime.on("get_missing_sales_orders", (data) => {
		frappe.hide_msgprint(true);
		frappe.show_progress('Syncing Sales Order..', data.progress, data.total, __("Fetching {0} of {1} invoices", [data.progress, data.total]), true);
		if (data.progress === data.total) {
			frm.reload_doc();
		}
	});
}

function handle_custom_buttons(frm) {
	if (!frm.is_new() && frm.doc.docstatus === 0) {
		frm.add_custom_button('Invoice Details', () => {
			fetch_invoice_details(frm);
		}, 'Fetch');

		frm.add_custom_button('Missing Sales Orders', async () => {
			await get_missing_sales_orders(frm);
		}, 'Fetch');

		// Button for debug purposes only for System Manager
		if (frappe.user.has_role('System Manager')) {
			frm.add_custom_button('Unset Ready to Process', () => {
				unset_ready_to_process(frm);
			}, 'Fetch');
		}

		let order_ids = get_pending_order_ids(frm);

		if (order_ids.length) {
			frm.add_custom_button(__("Reconcile Invoices"), () => {
				show_payment_reconciliation_dialog(frm);
			});
		}

	}
}

function fetch_invoice_details(frm) {
	frm.call({
		method: "fetch_invoice_details",
		doc: frm.doc,
		callback: function (r) {
			frappe.show_alert({
				message: __("Invoice Details fetched.."),
				indicator: "green",
			});
			frm.reload_doc();
		},
		freeze: true,
		freeze_message: __('Fetching Invoice Details...')
	});
}

/**
 * Function to continuously fetch missing sales orders
 * until all eligible order IDs are synced.
 */
async function get_missing_sales_orders(frm) {
	let has_more_orders = true;

	const records = await frappe.db.get_list("Amazon SP API Settings", {
		fields: ["name"],
		filters: { is_active: 1 },
	});

	const amz_setting_name = records.at(-1)?.name;

	if (!amz_setting_name) {
		frappe.msgprint("No active Amazon SP API Settings found.");
		return;
	}

	do {
		await frm.reload_doc();

		let amazon_order_ids = [];
		let count = 0;

		const max_invoice_count = await frappe.db.get_single_value(
			"eSeller Settings",
			"max_invoice_count"
		);

		for (const row of frm.doc.payment_details) {
			if (
				row.order_id &&
				row.ready_to_process == 0 &&
				row.ignore_transaction == 0 &&
				row.order_id.trim() !== "" &&
				(frm.doc.consider_so_only ? row.has_sales_order == 0 : true) &&
				count < max_invoice_count &&
				!amazon_order_ids.includes(row.order_id.trim())
			) {
				amazon_order_ids.push(row.order_id.trim());
				count++;
			}
		}

		// Stop if no more orders
		if (amazon_order_ids.length === 0) {
			break;
		}

		// Process current batch
		for (let i = 0; i < amazon_order_ids.length; i++) {
			frappe.show_progress(
				"Syncing Sales Order..",
				i + 1,
				amazon_order_ids.length,
				__("Fetching {0} of {1} invoices", [
					i + 1,
					amazon_order_ids.length,
				])
			);

			try {
				await frappe.call({
					method:
						"eseller_suite.eseller_suite.doctype.amazon_sp_api_settings.amazon_repository.get_order",
					args: {
						amz_setting_name: amz_setting_name,
						amazon_order_ids: amazon_order_ids[i],
						run_si_submit_job: 0,
					},
					freeze: true,
					freeze_message: __("Syncing Sales Order.."),
				});
			} catch (err) {
				console.error("Error fetching order:", err);
			}
		}

		// Run SI submit job after processing the batch
		await frm.call({
			method: "submit_invoices_in_background",
			doc: frm.doc,
			freeze: true,
			freeze_message: __("Fetching Invoice Details..."),
		});

		// Fetch invoice details after batch
		await frm.call({
			method: "fetch_invoice_details",
			doc: frm.doc,
			freeze: true,
			freeze_message: __("Fetching Invoice Details..."),
		});

		await frm.reload_doc();

		// Repeat only if consider_so_only is enabled
		has_more_orders = frm.doc.consider_so_only ? true : false;

	} while (has_more_orders);

	frappe.hide_progress();

	frappe.show_alert({
		message: __("Sales order syncing completed"),
		indicator: "green",
	});

	await frm.reload_doc();
}


/**
 * function to uncheck ready to process checks in all the lines in the table
 */
function unset_ready_to_process(frm) {
	frm.call({
		method: "unset_ready_to_process",
		doc: frm.doc,
		freeze: true,
		freeze_message: __("Removing Ready to Process.."),
		callback: (r) => {
			if (r && r.message) {
				frappe.show_alert({
					message: __('Ready to Process removed successfully'),
					indicator: 'green'
				}, 5);
			}
			frm.reload_doc();
		}
	});
}

function set_introduction_texts(frm) {
	if (frm.doc.docstatus === 0) {
		frm.set_intro('');
		const total_count = frm.doc.payment_details ? frm.doc.payment_details.length : 0;
		// Get rows where checkbox is NOT checked
		const remaining_rows = (frm.doc.payment_details || []).filter(
			row => !row.ready_to_process && !row.ignore_transaction
		);
		// Remaining unchecked rows count
		const remaining_count = remaining_rows.length;

		if (remaining_count === 0) {
			frm.set_intro('Amazon Payment Entry is ready to submit', 'green');
		}
		else {
			frm.set_intro(
				__('Please process all missing orders ({0} out of {1} remaining) before submitting.',
					[remaining_count, total_count]),
				'red'
			);
			frm.disable_save();
		}
	}
}

function show_payment_reconciliation_dialog(frm) {
	let order_ids = get_pending_order_ids(frm);

	if (!order_ids.length) {
		frappe.msgprint(__("No payment rows pending reconciliation."));
		return;
	}

	let dialog = new frappe.ui.Dialog({
		title: __("Payment Reconciliation"),
		size: "extra-large",
		fields: [
			{
				fieldname: "amazon_order_id",
				fieldtype: "Autocomplete",
				label: __("Amazon Order ID"),
				options: order_ids.join("\n"),
				default: order_ids[0],
				reqd: 1,
				change() {
					let order_id = dialog.get_value("amazon_order_id");

					if (order_id) {
						render_reconciliation_ui(frm, dialog, order_id);
					}
				}
			},
			{
				fieldname: "reconciliation_html",
				fieldtype: "HTML"
			}
		],
		primary_action_label: __("Reconcile"),
		primary_action() {
			reconcile_selected_rows(frm, dialog);
		}
	});

	dialog.show();
	render_reconciliation_ui(frm, dialog, order_ids[0]);
}

function get_pending_order_ids(frm) {
	return [
		...new Set(
			(frm.doc.payment_details || [])
				.filter(row =>
					cint(row.ready_to_process) === 0 &&
					cint(row.ignore_transaction) === 0
				)
				.map(row => row.order_id)
				.filter(Boolean)
		)
	];
}

function get_reconciled_invoice_ids(frm) {
	return new Set(
		(frm.doc.payment_details || [])
			.map(row => row.sales_invoice)
			.filter(Boolean)
	);
}

async function render_reconciliation_ui(frm, dialog, amazon_order_id) {
	let wrapper = dialog.fields_dict.reconciliation_html.$wrapper;

	wrapper.html(`
		<div class="text-muted" style="padding: 15px 0;">
			${__("Loading reconciliation data...")}
		</div>
	`);

	/*
		Filter pending Payment Details
		for selected Amazon Order ID
	*/

	let payment_rows = (frm.doc.payment_details || []).filter(row =>
		row.order_id === amazon_order_id &&
		cint(row.ready_to_process) === 0 &&
		cint(row.ignore_transaction) === 0
	);

	/*
		Get Sales Invoices already linked
		in any Payment Detail row
	*/

	let reconciled_invoice_ids = get_reconciled_invoice_ids(frm);

	/*
		Get submitted Sales Invoices
		for selected Amazon Order ID
	*/

	let invoices = await frappe.db.get_list(
		"Sales Invoice",
		{
			filters: {
				docstatus: 1,
				amazon_order_id: amazon_order_id
			},
			fields: [
				"name",
				"amazon_order_id",
				"grand_total",
				"company",
				"customer",
				"debit_to"
			],
			limit: 0
		}
	);

	/*
		Add reconciliation status
		to each invoice
	*/

	invoices = invoices.map(invoice => ({
		...invoice,
		is_reconciled: reconciled_invoice_ids.has(invoice.name)
	}));

	dialog._payment_rows = payment_rows;
	dialog._invoice_rows = invoices;

	let html = `
		<style>
			.reconciliation-container {
				display: grid;
				grid-template-columns: 1fr 1fr;
				gap: 20px;
				margin-top: 15px;
			}
			.reconciliation-panel {
				border: 1px solid var(--border-color);
				border-radius: 8px;
				overflow: hidden;
			}
			.reconciliation-header {
				padding: 12px 15px;
				font-weight: 600;
				font-size: 15px;
				background: var(--subtle-fg);
				border-bottom: 1px solid var(--border-color);
			}
			.reconciliation-table-wrapper {
				max-height: 450px;
				overflow-y: auto;
			}
			.reconciliation-table {
				margin-bottom: 0;
			}
			.reconciliation-table th {
				position: sticky;
				top: 0;
				background: var(--card-bg);
				z-index: 1;
			}
			.reconciled-invoice-row,
			.reconciled-invoice-row td {
				background-color: #d1e7dd !important;
			}
			.reconciliation-footer {
				display: flex;
				justify-content: space-between;
				padding: 12px 15px;
				font-weight: 600;
				border-top: 1px solid var(--border-color);
			}
			.reconciliation-summary {
				margin-top: 20px;
				padding: 15px;
				border: 1px solid var(--border-color);
				border-radius: 8px;
				display: flex;
				justify-content: space-between;
				align-items: center;
			}
			.reconciliation-difference {
				font-size: 16px;
				font-weight: 600;
			}
			@media (max-width: 900px) {
				.reconciliation-container {
					grid-template-columns: 1fr;
				}
			}
		</style>

		<div class="reconciliation-container">
			<!-- Payment Details -->
			<div class="reconciliation-panel">
				<div class="reconciliation-header">
					${__("Payment Details")}
				</div>
				<div class="reconciliation-table-wrapper">
					<table class="table table-bordered reconciliation-table">
						<thead>
							<tr>
								<th style="width: 40px;">
									<input type="checkbox" class="select-all-payments">
								</th>
								<th>
									${__("Sl")}
								</th>
								<th>
									${__("Transaction Type")}
								</th>
								<th class="text-right">
									${__("Amount")}
								</th>
							</tr>
						</thead>
						<tbody>
							${
								payment_rows.length

								? payment_rows.map(
									(row, index) => `
										<tr>
											<td>
												<input type="checkbox" class="payment-checkbox" data-index="${index}">
											</td>
											<td>
												${row.idx || ""}
											</td>
											<td>
												${frappe.utils.escape_html(row.transaction_type || "")}
											</td>
											<td class="text-right">
												${format_currency(flt(row.total))}
											</td>
										</tr>

									`
								).join("")

								: `
									<tr>
										<td colspan="4" class=" text-center text-muted">
											${
												__("No pending payment rows found.")
											}
										</td>
									</tr>
								`
							}
						</tbody>
					</table>
				</div>

				<div class="reconciliation-footer">
					<span>
						${__("Selected Total")}
					</span>
					<span class="payment-selected-total">
						${format_currency(0)}
					</span>
				</div>
			</div>

			<!-- Sales Invoices -->
			<div class="reconciliation-panel">
				<div class="reconciliation-header">
					${__("Sales Invoices")}
				</div>
				<div class="reconciliation-table-wrapper">
					<table class="table table-bordered reconciliation-table">
						<thead>
							<tr>
								<th style="width: 40px;"></th>
								<th>
									${__("Invoice ID")}
								</th>
								<th class="text-right">
									${__("Amount")}
								</th>
							</tr>
						</thead>
						<tbody>
							${
								invoices.length
								? invoices.map(
									(invoice, index) => `
										<tr
											class="${
												invoice.is_reconciled
													? "reconciled-invoice-row"
													: ""
											}"
										>
											<td>
												<input type="radio" name="selected_invoice" class="invoice-radio" data-index="${index}">
											</td>
											<td>
												<a href="/app/sales-invoice/${invoice.name}" target="_blank">
													${invoice.name}
												</a>
											</td>
											<td class="text-right">
												${format_currency(flt(invoice.grand_total))}
											</td>
										</tr>
									`
								).join("")

								: `
									<tr>
										<td colspan="3" class="text-center text-muted">
											${__("No submitted Sales Invoices found.")}
										</td>
									</tr>
								`
							}
						</tbody>
					</table>
				</div>

				<div class="reconciliation-footer">
					<span>
						${__("Selected Total")}
					</span>
					<span class="invoice-selected-total">
						${format_currency(0)}
					</span>
				</div>
			</div>
		</div>

		<div class="reconciliation-summary">
			<div>
				${__(
					"Select payment rows and a Sales Invoice to reconcile."
				)}
			</div>
			<div class="reconciliation-difference">
				${__("Difference")}:
				<span class="difference-value">
					${format_currency(0)}
				</span>
			</div>
		</div>
	`;

	wrapper.html(html);
	setup_reconciliation_events(dialog);
	update_reconciliation_totals(dialog);
}

function setup_reconciliation_events(dialog) {
	let wrapper = dialog.fields_dict.reconciliation_html.$wrapper;

	/*
		Select all payment rows
	*/

	wrapper.on("change", ".select-all-payments", function () {
			let checked = $(this).is(":checked");

			wrapper.find(".payment-checkbox").prop("checked", checked);

			update_reconciliation_totals(dialog);
		}
	);


	/*
		Individual Payment Detail selection
	*/

	wrapper.on(
		"change",
		".payment-checkbox",
		function () {

			let total = wrapper
				.find(".payment-checkbox")
				.length;

			let selected = wrapper
				.find(".payment-checkbox:checked")
				.length;

			wrapper
				.find(".select-all-payments")
				.prop(
					"checked",
					total > 0 && total === selected
				);

			update_reconciliation_totals(dialog);
		}
	);


	/*
		Sales Invoice selection
	*/

	wrapper.on(
		"change",
		".invoice-radio",
		function () {
			update_reconciliation_totals(dialog);
		}
	);
}

function update_reconciliation_totals(dialog) {
	let wrapper = dialog.fields_dict.reconciliation_html.$wrapper;
	let payment_total = 0;
	let invoice_total = 0;

	wrapper
		.find(".payment-checkbox:checked")
		.each(function () {

			let index = cint($(this).attr("data-index"));

			let row = dialog._payment_rows[index];
			payment_total += flt(row.total);
		});

	let selected_invoice = wrapper.find(".invoice-radio:checked");

	if (selected_invoice.length) {
		let index = cint(selected_invoice.attr("data-index"));

		let invoice = dialog._invoice_rows[index];

		invoice_total = flt(invoice.grand_total);
	}

	let difference = flt(payment_total - invoice_total, 2);

	wrapper
		.find(".payment-selected-total")
		.text(format_currency(payment_total));

	wrapper
		.find(".invoice-selected-total")
		.text(format_currency(invoice_total));

	wrapper
		.find(".difference-value")
		.text(format_currency(difference));

	dialog._payment_total = payment_total;
	dialog._invoice_total = invoice_total;
	dialog._difference = difference;

	update_reconcile_button_state(dialog, difference);
}

function update_reconcile_button_state(dialog, difference) {
	let wrapper = dialog.fields_dict.reconciliation_html.$wrapper;

	let primary_button = dialog.get_primary_btn();

	let has_payments = wrapper.find(".payment-checkbox:checked").length > 0;

	let has_invoice = wrapper.find(".invoice-radio:checked").length > 0;

	/*
		Reset button style
	*/

	primary_button
		.removeClass("btn-danger")
		.addClass("btn-primary");

	/*
		Disable until both Payment Details
		and Sales Invoice are selected
	*/

	if (!has_payments || !has_invoice) {
		primary_button.prop("disabled", true);
		return;
	}


	/*
		Exact Match

		Enable button and keep default
		black primary button
	*/

	if (difference === 0) {
		primary_button
			.prop("disabled", false)
			.removeClass("btn-danger")
			.addClass("btn-primary");

		return;
	}


	/*
		Any Difference

		Enable button and make it red
	*/

	primary_button
		.prop("disabled", false)
		.removeClass("btn-primary")
		.addClass("btn-danger");
}

function reconcile_selected_rows(frm, dialog) {
	let wrapper = dialog.fields_dict.reconciliation_html.$wrapper;

	let selected_payment_indexes = [];

	wrapper
		.find(".payment-checkbox:checked")
		.each(function () {
			selected_payment_indexes.push(
				cint($(this).attr("data-index"))
			);

		});


	let selected_invoice = wrapper.find(".invoice-radio:checked");

	if (!selected_payment_indexes.length) {
		frappe.msgprint(
			__("Please select at least one Payment Detail.")
		);

		return;
	}


	if (!selected_invoice.length) {
		frappe.msgprint(
			__("Please select a Sales Invoice.")
		);

		return;
	}

	let invoice_index = cint(selected_invoice.attr("data-index"));

	let invoice = dialog._invoice_rows[invoice_index];

	let payment_total = selected_payment_indexes.reduce((total, index) => {
				return total + flt(dialog._payment_rows[index].total);
			},
			0
		);


	let invoice_total = flt(invoice.grand_total);
	let difference = flt(payment_total - invoice_total, 2);

	/*
		Any Non-Zero Difference

		Show warning and ask for confirmation
	*/

	if (difference !== 0) {
		frappe.confirm(
			`
				<strong>
					${__("Warning: Amount Difference")}
				</strong>

				<br><br>

				${__("Payment Total")}:
				<strong>
					${format_currency(payment_total)}
				</strong>

				<br>

				${__("Invoice Total")}:
				<strong>
					${format_currency(invoice_total)}
				</strong>

				<br>

				${__("Difference")}:
				<strong>
					${format_currency(difference)}
				</strong>

				<br><br>

				${__(
					"The payment total and invoice total do not match. Do you want to continue reconciliation?"
				)}
			`,
			() => {
				complete_reconciliation(frm, dialog, selected_payment_indexes, invoice);
			}
		);

		return;
	}

	complete_reconciliation(frm, dialog, selected_payment_indexes, invoice);
}

function complete_reconciliation(frm, dialog, selected_payment_indexes, invoice) {
	selected_payment_indexes.forEach(index => {
		let row = dialog._payment_rows[index];
		frappe.model.set_value(row.doctype, row.name, "sales_invoice", invoice.name);
		frappe.model.set_value(row.doctype, row.name, "customer", invoice.customer);
		frappe.model.set_value(row.doctype, row.name, "company", invoice.company);
		frappe.model.set_value(row.doctype, row.name, "default_receivable_account", invoice.debit_to);
		frappe.model.set_value(row.doctype, row.name, "ready_to_process", 1);
	});

	frm.dirty();
	frm.refresh_field("payment_details");

	frm.save().then(() => {
		frappe.show_alert({
			message: __(
				"Successfully reconciled {0} payment row(s) with {1}.", [selected_payment_indexes.length, invoice.name]
			),
			indicator: "green"
		});

		let remaining_order_ids = get_pending_order_ids(frm);

		if (!remaining_order_ids.length) {
			dialog.hide();
			return;
		}

		dialog.fields_dict.amazon_order_id.df.options =remaining_order_ids.join("\n");

		dialog.fields_dict.amazon_order_id.refresh();

		let current_order_id = dialog.get_value("amazon_order_id");

		let next_order_id = remaining_order_ids.includes(current_order_id) ? current_order_id : remaining_order_ids[0];

		dialog.set_value("amazon_order_id", next_order_id);

		render_reconciliation_ui(frm, dialog, next_order_id);
	});
}
