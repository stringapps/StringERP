"""Sales sync API used by the external POS.

Endpoints (all POST, token auth):
	create_siv              - Sales Invoice / Debit Note / Sales Order (+ advance payments);
	                          an invoice with "soName" is linked to that Sales Order
	cancel_sales_order      - cancel a Sales Order and its Payment Entries
	add_sales_order_payment - record a later (part / split) payment against a Sales Order
	refund_sales_order      - pay a refund to the customer of a cancelled Sales Order
	create_invoice_closing  - create / submit an Invoice Closing for a POS Profile and date
	get_customer_details    - read a Customer

Every write endpoint runs in one transaction: on failure it rolls back, logs the error,
sets an HTTP error status and returns {"status": "error", "message": ...}.
"""

from datetime import date

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account
from erpnext.accounts.party import get_party_account
from frappe import _
from frappe.utils import flt, today

from stringerp.v1.utils import api_log, bill_type_map

REFUND_REMARK = "Refund against cancelled Sales Order {0}"

# Each write endpoint opens this savepoint first, so a failure undoes only that call's work
SAVEPOINT = "stringerp_api"

# ordertype sent by the POS -> label used in logs
ORDER_TYPES = {"Invoice": "Invoice", "Debit note": "Debit note", "Order": "Order"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_payload(kwargs):
	"""Accept either form fields or a JSON string sent in a `data` field."""
	if isinstance(kwargs.get("data"), str):
		return frappe.parse_json(kwargs.get("data"))
	return kwargs


def _is_concurrency_error(e):
	"""True when another request changed the same rows at the same time.

	Frappe wraps MariaDB's 1020 "Record has changed since last read" (snapshot isolation) and
	1213 deadlock as QueryDeadlockError, and 1205 lock wait timeout as QueryTimeoutError.
	"""
	return isinstance(e, frappe.QueryDeadlockError | frappe.QueryTimeoutError)


def _fail(api, kwargs, e, title=None):
	"""Undo this call's writes, log, set the HTTP status and build the error response."""
	concurrent = _is_concurrency_error(e)
	try:
		if concurrent:
			raise RuntimeError  # the DB may have aborted the transaction: roll it all back
		frappe.db.rollback(save_point=SAVEPOINT)
	except Exception:
		frappe.db.rollback()

	if concurrent:
		frappe.local.response.http_status_code = 409
		message = _("Another request changed the same records at the same time. Please retry.")
	else:
		_set_error_status(e)
		message = str(e)

	frappe.log_error(frappe.get_traceback(), title or f"{api} Failed")
	api_log(api=api, data=kwargs, response=None, status="Failed", error=str(e))
	return {"status": "error", "message": message}


def _set_error_status(e):
	"""Set the HTTP status for a failed API call.

	Frappe exceptions carry their own code (PermissionError 403, DoesNotExistError 404,
	ValidationError 417). Any other exception also gets 417, the code frappe.throw uses.
	"""
	frappe.local.response.http_status_code = getattr(e, "http_status_code", None) or 417


def _as_bool(value):
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	if isinstance(value, int | float):
		return value == 1
	if isinstance(value, str):
		return value.strip().lower() in {"1", "true", "yes", "y", "on"}
	return False


def _date_part(value):
	"""'2026-03-22 11:50:42' -> '2026-03-22'; tolerates None."""
	return (value or "")[:10]


def _time_part(value):
	"""'2026-03-22 11:50:42' -> '11:50:42'; tolerates None."""
	return (value or "")[11:]


def _map_item(row):
	"""Map one POS item / delivery-charge row to a Sales Invoice / Sales Order item."""
	unit_price = flt(row.get("UnitPrice", 0))
	unit_disc = flt(row.get("UnitDisc", 0))
	return {
		"item_code": row.get("barcode"),
		"qty": flt(row.get("quantity", 1)),
		"price_list_rate": flt(row.get("rate", 0)),
		"discount_percentage": unit_disc / unit_price * 100 if unit_price else 0,
		"custom_discount_rate": unit_disc,
		"custom_discount_type": row.get("DiscTID"),
	}


def _map_items(external_data):
	rows = (external_data.get("items") or []) + (external_data.get("deliveryCharges") or [])
	return [_map_item(row) for row in rows]


def _customer_by_code(customer_code):
	if not customer_code:
		raise frappe.ValidationError(_("Customer code is missing"))
	customer = frappe.db.get_value("Customer", {"custom_customer_code": customer_code}, "name")
	if not customer:
		raise frappe.ValidationError(_("Customer with customer code {0} is missing").format(customer_code))
	return customer


def _resolve_order_customer(kwargs):
	"""Customer for a Sales Order.

	With customercode: looked up by code, the same as for invoices (no customer is created).
	Without it (walk-in orders): matched on firstName, created if it does not exist yet.
	"""
	if kwargs.get("customercode"):
		return _customer_by_code(kwargs.get("customercode"))
	name = kwargs.get("firstName")
	if not name:
		raise frappe.ValidationError(_("Send customercode or firstName"))
	if frappe.db.exists("Customer", name):
		return name
	return create_customer(kwargs)


def _resolve_company(external_data):
	"""Company from erp_comp_name, else from the POS Profile."""
	company = external_data.get("erp_comp_name")
	if not company and external_data.get("posProfile"):
		company = frappe.db.get_value("POS Profile", external_data.get("posProfile"), "company")
	if not company:
		raise frappe.ValidationError(_("Company is required: send erp_comp_name or a valid posProfile"))
	if not frappe.db.exists("Company", company):
		raise frappe.ValidationError(_("Company {0} does not exist").format(company))
	return company


def _sales_team(salesman_code):
	"""Sales team rows for a POS salesman code (Sales Person.custom_sales_person_code)."""
	if not salesman_code:
		return []
	sales_person = frappe.db.exists("Sales Person", {"custom_sales_person_code": salesman_code})
	if not sales_person:
		raise frappe.ValidationError(_("Sales Person {0} is missing").format(salesman_code))
	return [{"sales_person": sales_person, "allocated_percentage": 100}]


def _lock(doctype, name):
	"""Row lock held until the request's transaction ends.

	Serialises concurrent requests for the same record, so check-then-insert logic
	(duplicate guid, refund limit) cannot be passed by two requests at once.
	"""
	frappe.db.get_value(doctype, name, "name", for_update=True)


def _ensure_unique(doctype, kwargs, label):
	"""Reject a guid / customer order number that already exists.

	Call after _lock("Customer", ...). The reads are locking reads (for_update) so they see
	an order committed by a concurrent request that held the customer lock first.
	"""
	guid = kwargs.get("guid")
	if guid and frappe.db.get_value(doctype, {"custom_guid": guid}, "name", for_update=True):
		raise frappe.ValidationError(_("{0} already exists with GUID: {1}").format(label, guid))
	order_no = kwargs.get("customerOrdernumber")
	if order_no and frappe.db.get_value(
		doctype, {"custom_customer_order_no": order_no}, "name", for_update=True
	):
		raise frappe.ValidationError(_("Order already exists with Customer Order No: {0}").format(order_no))


# ---------------------------------------------------------------------------
# create_siv: Sales Invoice / Debit Note / Sales Order
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=False)
def create_siv(**kwargs):
	kwargs = _parse_payload(kwargs)
	order_type = ORDER_TYPES.get(kwargs.get("ordertype"))
	try:
		frappe.db.savepoint(SAVEPOINT)
		if not order_type:
			raise frappe.ValidationError(_("Invalid ordertype"))
		if order_type == "Order":
			return create_sord(kwargs)
		return create_invoice(kwargs)
	except Exception as e:
		# Rolling back also undoes an auto-created customer, the order and any payments,
		# so a retry with the same guid is not rejected as a duplicate.
		api = f"Create Sales {order_type}" if order_type else "Create Sales"
		title = f"{order_type} Sync Failed" if order_type else "Sync Failed"
		return _fail(api, kwargs, e, title=title)


def create_invoice(kwargs):
	# Same guid is always sent for the same customer: locking the customer row makes
	# concurrent retries wait here, so the duplicate check below sees the first one.
	_lock("Customer", _customer_by_code(kwargs.get("customercode")))

	mapped_data = map_external_to_sales_invoice(kwargs)

	if kwargs.get("Reupload"):
		existing_invoice = frappe.db.exists(
			"Sales Invoice", {"custom_guid": kwargs.get("guid"), "docstatus": 0}
		)
		if not existing_invoice:
			raise frappe.ValidationError(
				_("Reupload: No draft invoice found with GUID: {0}").format(kwargs.get("guid"))
			)
		doc = frappe.get_doc("Sales Invoice", existing_invoice)
	else:
		_ensure_unique("Sales Invoice", kwargs, _("Invoice"))
		doc = frappe.new_doc("Sales Invoice")

	doc.update(mapped_data)
	sales_order = _link_sales_order(doc, kwargs)
	doc.set_taxes()
	doc.calculate_taxes_and_totals()
	doc.update_stock = 1
	if kwargs.get("ordertype") == "Debit note":
		doc.is_debit_note = 1
	doc.save(ignore_permissions=True)
	if sales_order:
		# Same as ERPNext's make_sales_invoice: pull the order's advance Payment Entries.
		# ERPNext skips auto-allocation for POS invoices, so do it explicitly. Done after the
		# first save, because taxes are only applied there and allocation is capped at the total.
		doc.set_advances()
		if not doc.payments:
			# ERPNext won't submit a POS invoice without a payment row, even when the
			# order's advances cover it in full; add the POS Profile's default mode at 0.
			doc.append("payments", {"mode_of_payment": _default_mode_of_payment(doc.pos_profile), "amount": 0})
		doc.save(ignore_permissions=True)

	invoice = frappe.db.get_value("Sales Invoice", doc.name, "*")
	invoice.items = frappe.db.get_all("Sales Invoice Item", {"parent": doc.name}, "*")
	invoice.payments = frappe.db.get_all("Sales Invoice Payment", {"parent": doc.name}, "*")
	invoice.taxes = frappe.db.get_all("Sales Taxes and Charges", {"parent": doc.name}, "*")

	response = {"status": "success", "invoice": invoice}
	api_log(api="Create Sales Invoice", data=kwargs, response=str(response), status="Success")
	return response


def _default_mode_of_payment(pos_profile):
	modes = frappe.get_all(
		"POS Payment Method",
		filters={"parent": pos_profile, "parenttype": "POS Profile"},
		fields=["mode_of_payment", "default"],
		order_by="`default` desc, idx asc",
		limit=1,
	)
	if not modes:
		raise frappe.ValidationError(_("POS Profile {0} has no mode of payment").format(pos_profile))
	return modes[0].mode_of_payment


def _find_order_for_invoice(doc, kwargs):
	"""Sales Order named in payload "soName", or None for an independent invoice."""
	so_name = kwargs.get("soName")
	if not so_name:
		return None
	so = frappe.db.get_value("Sales Order", so_name, ["docstatus", "customer"], as_dict=True)
	if not so:
		raise frappe.DoesNotExistError(_("Sales Order {0} not found").format(so_name))
	if so.docstatus != 1:
		raise frappe.ValidationError(_("Sales Order {0} is not submitted").format(so_name))
	if so.customer != doc.customer:
		raise frappe.ValidationError(
			_("Sales Order {0} belongs to customer {1}, not {2}").format(so_name, so.customer, doc.customer)
		)
	return so_name


def _link_sales_order(doc, kwargs):
	"""Link invoice items to their Sales Order rows (sales_order / so_detail), as ERPNext's
	"Create > Sales Invoice" from a Sales Order does. This makes ERPNext update the order's
	billed % / status on submit and lets set_advances() find the order's advance payments.

	Each invoice item is matched by item_code to an order row that still has unbilled qty
	(qty already on non-cancelled invoices is excluded). Items with no match stay unlinked.
	"""
	so_name = _find_order_for_invoice(doc, kwargs)
	if not so_name:
		return None
	_lock("Sales Order", so_name)
	so = frappe.get_doc("Sales Order", so_name)
	if so.status == "Closed" or so.per_billed >= 100:
		raise frappe.ValidationError(_("Sales Order {0} is already fully billed or closed").format(so.name))

	billed = {
		r.so_detail: flt(r.qty)
		for r in frappe.db.sql(
			"""select sii.so_detail, sum(sii.qty) as qty
			from `tabSales Invoice Item` sii join `tabSales Invoice` si on si.name = sii.parent
			where sii.sales_order = %(so)s and si.docstatus < 2 and si.name != %(si)s
			group by sii.so_detail""",
			{"so": so.name, "si": doc.name or ""},
			as_dict=True,
		)
	}
	remaining = {row.name: flt(row.qty) - billed.get(row.name, 0) for row in so.items}

	linked = False
	for item in doc.items:
		for row in so.items:
			if row.item_code == item.item_code and remaining[row.name] > 0:
				item.sales_order = so.name
				item.so_detail = row.name
				remaining[row.name] -= flt(item.qty)
				linked = True
				break

	if not linked:
		raise frappe.ValidationError(
			_("No invoice item matches an unbilled item of Sales Order {0}").format(so.name)
		)
	return so.name


def map_external_to_sales_invoice(external_data):
	"""Map incoming external POS data to ERPNext Sales Invoice format.

	Totals are not mapped: calculate_taxes_and_totals() recomputes them from the items.
	"""
	mapped = {
		"company": _resolve_company(external_data),
		"customer": frappe.db.get_value(
			"Customer", {"custom_customer_code": external_data.get("customercode")}, "name"
		),
		"ignore_pricing_rule": 1,
		"custom_walkin_customer_name": external_data.get("custname"),
		"set_posting_time": 1,
		"posting_date": _date_part(external_data.get("CRTime")),
		"posting_time": _time_part(external_data.get("CRTime")),
		"due_date": _date_part(external_data.get("billDate")),
		"remarks": external_data.get("Remarks"),
		"custom_customer_order_no": external_data.get("customerOrdernumber"),
		"custom_custom_name": 1,
		"items": _map_items(external_data),
		"payments": [],
		"is_pos": 1,
		"is_return": 0,
		"is_paid": bool(external_data.get("ispaid")),
		# Additional discount, deducted from the net total
		"apply_discount_on": "Net Total",
		"discount_amount": flt(external_data.get("discountAmount", 0)),
		"additional_discount_account": external_data.get("additionalDiscountGL") or None,
		"custom_walkin_customer_alt_phone": external_data.get("altPhone") or "",
		"custom_walkin_customer_phone": external_data.get("phoneNumber") or "",
		"custom_walkin_customer_address": external_data.get("address1") or "",
		"custom_delivery_man": external_data.get("deliverymancode") or "",
		"pos_profile": external_data.get("posProfile") or "",
		"custom_bill_type": bill_type_map.get(external_data.get("salestype")) or "",
		"custom_guid": external_data.get("guid") or "",
		"custom_no_of_pax": external_data.get("noOfPax"),
		"custom_order_remarks": external_data.get("Remarks"),
		"custom_table_no": external_data.get("tableno"),
		"disable_rounded_total": True,
		"sales_team": [],
		"custom_online_order_id": external_data.get("onlineOrderId"),
	}

	mapped["sales_team"] = _sales_team(external_data.get("salesmancode"))

	if external_data.get("ispaid"):
		for pay in external_data.get("payments") or []:
			mapped["payments"].append(
				{
					"mode_of_payment": pay.get("CARDTYPE", "Cash"),
					"amount": flt(pay.get("AMOUNT", 0)),
					"reference_no": pay.get("CARDNO", ""),
				}
			)

	return mapped


def create_sord(kwargs):
	customer = _resolve_order_customer(kwargs)
	_lock("Customer", customer)
	_ensure_unique("Sales Order", kwargs, _("Order"))

	doc = frappe.new_doc("Sales Order")
	doc.update(map_external_to_sales_order(kwargs))
	doc.customer = customer
	doc.insert(ignore_permissions=True)
	doc.submit()

	payments = create_sales_order_payments(doc, kwargs)
	paid_amount = sum(flt(p.get("paid_amount")) for p in payments)

	response = {
		"status": "success",
		"order": doc,
		"payment": payments[0] if len(payments) == 1 else payments,
		"paid_amount": paid_amount,
		"outstanding_amount": flt(doc.grand_total) - paid_amount,
	}
	api_log(api="Create Sales Order", data=kwargs, response=str(response), status="Success")
	return response


def create_sales_order_payments(so_doc, kwargs):
	"""Create advance Payment Entries against a submitted Sales Order.

	- Each line in kwargs["payments"] (AMOUNT / CARDTYPE / CARDNO) becomes its own
	  Payment Entry, so partial advances (e.g. 500 paid on a 1000 order) are supported.
	- The total of the payment lines may not exceed the order's grand total.
	- If "ispaid" is set and no payment lines are sent, the full order amount is paid.
	"""
	payment_lines = [p for p in (kwargs.get("payments") or []) if flt(p.get("AMOUNT")) > 0]

	if not payment_lines:
		if not kwargs.get("ispaid"):
			return []
		payment_lines = [{"AMOUNT": so_doc.grand_total, "CARDTYPE": None, "CARDNO": None}]

	total_requested = sum(flt(p.get("AMOUNT")) for p in payment_lines)
	if total_requested > flt(so_doc.grand_total):
		raise frappe.ValidationError(
			_("Payment amount {0} exceeds Sales Order total {1}").format(total_requested, so_doc.grand_total)
		)

	return [
		_create_order_payment(so_doc, flt(pay.get("AMOUNT")), pay.get("CARDTYPE"), pay.get("CARDNO")).as_dict()
		for pay in payment_lines
	]


def _create_order_payment(so_doc, amount, mode_of_payment=None, reference_no=None, guid=None, posting_date=None):
	"""Submit one 'Receive' Payment Entry of `amount` allocated to the Sales Order as an advance."""
	pe_doc = get_payment_entry(dt="Sales Order", dn=so_doc.name, party_amount=amount)

	if mode_of_payment:
		pe_doc.mode_of_payment = mode_of_payment
		account = get_bank_cash_account(mode_of_payment, so_doc.company).get("account")
		if account:
			pe_doc.paid_to = account

	pe_doc.paid_amount = amount
	pe_doc.received_amount = amount
	for ref in pe_doc.references:
		ref.allocated_amount = amount

	if posting_date:
		pe_doc.posting_date = posting_date
	pe_doc.reference_no = reference_no or so_doc.name
	pe_doc.reference_date = posting_date or so_doc.transaction_date
	if guid:
		pe_doc.custom_guid = guid
	pe_doc.insert()
	pe_doc.submit()
	return pe_doc


def map_external_to_sales_order(external_data):
	order_date = _date_part(external_data.get("CRTime")) or date.today()
	# customer is set by create_sord (see _resolve_order_customer)
	company = _resolve_company(external_data)
	warehouse = _resolve_order_warehouse(external_data, company)
	items = _map_items(external_data)
	for item in items:
		item["warehouse"] = warehouse
	return {
		"company": company,
		"set_warehouse": warehouse,
		"custom_walkin_customer_address": external_data.get("address1") or "",
		"transaction_date": order_date,
		"delivery_date": order_date,
		"remarks": external_data.get("Remarks"),
		"custom_customer_order_no": external_data.get("customerOrdernumber"),
		"items": items,
		"discount_amount": flt(external_data.get("SubTotalDiscount", 0)),
		"custom_guid": external_data.get("guid") or "",
		"disable_rounded_total": True,
		"sales_team": _sales_team(external_data.get("salesmancode")),
	}


def _resolve_order_warehouse(external_data, company):
	"""Source warehouse for a Sales Order: payload "warehouse", else the POS Profile's warehouse."""
	warehouse = external_data.get("warehouse")
	if not warehouse and external_data.get("posProfile"):
		warehouse = frappe.db.get_value("POS Profile", external_data.get("posProfile"), "warehouse")
	if not warehouse:
		raise frappe.ValidationError(_("Warehouse is required: send warehouse or a valid posProfile"))
	if frappe.db.get_value("Warehouse", warehouse, "company") != company:
		raise frappe.ValidationError(
			_("Warehouse {0} does not belong to company {1}").format(warehouse, company)
		)
	return warehouse


def create_customer(kwargs):
	customer = frappe.new_doc("Customer")
	customer.customer_name = kwargs.get("firstName")
	customer.tax_category = "Standard VAT"
	customer.insert()
	return customer.name


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=False)
def get_customer_details(customer_id):
	"""Return basic details of a Customer.

	Example: /api/method/stringerp.v1.api.get_customer_details?customer_id=CUST-0001
	"""
	try:
		if not customer_id:
			frappe.local.response.http_status_code = 400
			return {"status": "error", "message": _("Missing required parameter: customer_id")}

		customer = frappe.get_doc("Customer", customer_id)
		return {
			"status": "success",
			"message": _("Customer fetched successfully"),
			"data": {
				"name": customer.name,
				"customer_name": customer.customer_name,
				"customer_group": customer.customer_group,
				"territory": customer.territory,
				"mobile_no": customer.mobile_no,
				"email_id": customer.email_id,
			},
		}

	except frappe.DoesNotExistError:
		frappe.local.response.http_status_code = 404
		return {"status": "error", "message": _("Customer not found")}

	except frappe.PermissionError:
		frappe.local.response.http_status_code = 403
		return {"status": "error", "message": _("You do not have permission to access this resource.")}

	except Exception as e:
		frappe.log_error(message=frappe.get_traceback(), title="API Error: get_customer_details")
		frappe.local.response.http_status_code = 500
		return {
			"status": "error",
			"message": _("An unexpected error occurred. Please contact support."),
			"error": str(e) if frappe.conf.developer_mode else None,
		}


# ---------------------------------------------------------------------------
# Cancel / refund Sales Order
# ---------------------------------------------------------------------------


def _find_sales_order(kwargs):
	"""Resolve a Sales Order from sales_order, guid or customerOrdernumber.

	For guid / customerOrdernumber a non-cancelled order is preferred over a cancelled one.
	"""
	so_name = kwargs.get("sales_order")
	if so_name:
		if not frappe.db.exists("Sales Order", so_name):
			raise frappe.DoesNotExistError(_("Sales Order {0} not found").format(so_name))
		return so_name

	for key, field in (("guid", "custom_guid"), ("customerOrdernumber", "custom_customer_order_no")):
		value = kwargs.get(key)
		if value:
			name = frappe.db.get_value(
				"Sales Order", {field: value, "docstatus": ["!=", 2]}, "name"
			) or frappe.db.get_value("Sales Order", {field: value}, "name")
			if not name:
				raise frappe.DoesNotExistError(_("No Sales Order found with {0}: {1}").format(key, value))
			return name

	raise frappe.ValidationError(_("Send one of: sales_order, guid, customerOrdernumber"))


def _submitted_documents_against(so_name):
	"""Submitted Sales Invoices / Delivery Notes created from this Sales Order."""
	sources = (
		("Sales Invoice", "Sales Invoice Item", "sales_order"),
		("Delivery Note", "Delivery Note Item", "against_sales_order"),
	)
	found = []
	for doctype, child, link_field in sources:
		parents = frappe.get_all(
			child, filters={link_field: so_name, "docstatus": 1}, pluck="parent", distinct=True
		)
		found += [f"{doctype} {name}" for name in parents]
	return found


@frappe.whitelist(allow_guest=False)
def cancel_sales_order(**kwargs):
	"""Cancel a submitted Sales Order and every submitted Payment Entry linked to it.

	Identify the order with one of:
		sales_order          - Sales Order name
		guid                 - custom_guid sent at creation
		customerOrdernumber  - custom_customer_order_no sent at creation

	Everything runs in one transaction: if any step fails, nothing is cancelled.
	"""
	kwargs = _parse_payload(kwargs)
	try:
		frappe.db.savepoint(SAVEPOINT)
		so = frappe.get_doc("Sales Order", _find_sales_order(kwargs))

		if so.docstatus == 2:
			raise frappe.ValidationError(_("Sales Order {0} is already cancelled").format(so.name))
		if so.docstatus == 0:
			raise frappe.ValidationError(
				_("Sales Order {0} is a draft and cannot be cancelled").format(so.name)
			)

		blockers = _submitted_documents_against(so.name)
		if blockers:
			raise frappe.ValidationError(
				_("Cannot cancel Sales Order {0}; cancel these first: {1}").format(
					so.name, ", ".join(blockers)
				)
			)

		pe_names = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Sales Order", "reference_name": so.name, "docstatus": 1},
			pluck="parent",
			distinct=True,
		)

		cancelled_payments = []
		for pe_name in pe_names:
			pe = frappe.get_doc("Payment Entry", pe_name)
			others = [
				f"{r.reference_doctype} {r.reference_name}"
				for r in pe.references
				if not (r.reference_doctype == "Sales Order" and r.reference_name == so.name)
			]
			if others:
				raise frappe.ValidationError(
					_("Payment Entry {0} is also allocated to {1}; cancel it manually").format(
						pe.name, ", ".join(others)
					)
				)
			pe.flags.ignore_permissions = True
			pe.cancel()
			cancelled_payments.append(
				{"name": pe.name, "paid_amount": pe.paid_amount, "mode_of_payment": pe.mode_of_payment}
			)

		so.reload()
		so.flags.ignore_permissions = True
		so.cancel()

		response = {
			"status": "success",
			"message": _("Sales Order {0} cancelled").format(so.name),
			"sales_order": so.name,
			"cancelled_payments": cancelled_payments,
		}
		api_log(api="Cancel Sales Order", data=kwargs, response=response, status="Success")
		return response

	except Exception as e:
		return _fail("Cancel Sales Order", kwargs, e)


def _paid_on_cancelled_order(so_name):
	"""Total of the cancelled 'Receive' Payment Entries that were allocated to this order.

	A cancelled entry that was later amended is skipped: only the latest version of a
	payment counts, so cancel-and-amend does not inflate the refundable amount.
	"""
	result = frappe.db.sql(
		"""
		select sum(pe.paid_amount)
		from `tabPayment Entry` pe
		where pe.docstatus = 2
			and pe.payment_type = 'Receive'
			and exists (
				select 1 from `tabPayment Entry Reference` r
				where r.parent = pe.name
					and r.reference_doctype = 'Sales Order'
					and r.reference_name = %s
			)
			and not exists (
				select 1 from `tabPayment Entry` amended where amended.amended_from = pe.name
			)
		""",
		so_name,
	)
	return flt(result[0][0])


def _refunded_on_order(so):
	"""Total of submitted refunds linked to this order.

	Refunds are linked through Payment Entry.custom_refund_against_sales_order. The remark
	check only picks up refunds made before that field existed.

	`for update` makes this a locking read, so it sees refunds committed by a request that
	held the order lock before us (a plain read would use this transaction's older snapshot).
	"""
	result = frappe.db.sql(
		"""
		select sum(paid_amount)
		from `tabPayment Entry`
		where docstatus = 1
			and payment_type = 'Pay'
			and party_type = 'Customer'
			and party = %(customer)s
			and (custom_refund_against_sales_order = %(so)s or remarks = %(remark)s)
		for update
		""",
		{"customer": so.customer, "so": so.name, "remark": REFUND_REMARK.format(so.name)},
	)
	return flt(result[0][0])


def _refund_account(mode_of_payment, company):
	account = None
	if mode_of_payment:
		account = get_bank_cash_account(mode_of_payment, company).get("account")
	account = account or frappe.get_cached_value("Company", company, "default_cash_account")
	if not account:
		raise frappe.ValidationError(
			_("No cash/bank account found for mode of payment {0}").format(mode_of_payment)
		)
	return account


@frappe.whitelist(allow_guest=False)
def refund_sales_order(**kwargs):
	"""Pay a refund to the customer of an already-cancelled Sales Order.

	Identify the order with one of: sales_order, guid, customerOrdernumber.
	Other parameters:
		refund_amount    (float, required) - amount paid back to the customer (after deductions)
		mode_of_payment  (str, optional)   - paid out from this mode's default account
		reference_no     (str, optional)   - cheque / card / transaction no (default: order name)

	The refund cannot exceed what the customer paid on the order (its cancelled
	Payment Entries) minus refunds already made against it.
	"""
	kwargs = _parse_payload(kwargs)
	try:
		frappe.db.savepoint(SAVEPOINT)
		so_name = _find_sales_order(kwargs)
		# Held until commit: a second refund for the same order waits here and then
		# sees this refund in _refunded_on_order.
		_lock("Sales Order", so_name)
		so = frappe.get_doc("Sales Order", so_name)
		if so.docstatus != 2:
			raise frappe.ValidationError(
				_("Sales Order {0} is not cancelled; cancel it before refunding").format(so.name)
			)

		refund_amount = flt(kwargs.get("refund_amount"))
		if refund_amount <= 0:
			raise frappe.ValidationError(_("refund_amount must be greater than 0"))

		paid_on_order = _paid_on_cancelled_order(so.name)
		if not paid_on_order:
			raise frappe.ValidationError(
				_("No cancelled payment found for Sales Order {0}; nothing to refund").format(so.name)
			)

		already_refunded = _refunded_on_order(so)
		refundable = paid_on_order - already_refunded
		if refund_amount > refundable:
			raise frappe.ValidationError(
				_("Refund {0} exceeds refundable amount {1} (paid {2}, already refunded {3})").format(
					refund_amount, refundable, paid_on_order, already_refunded
				)
			)

		mode_of_payment = kwargs.get("mode_of_payment")
		pe = frappe.new_doc("Payment Entry")
		pe.update(
			{
				"payment_type": "Pay",
				"company": so.company,
				"posting_date": today(),
				"mode_of_payment": mode_of_payment,
				"party_type": "Customer",
				"party": so.customer,
				"paid_from": _refund_account(mode_of_payment, so.company),
				"paid_to": get_party_account("Customer", so.customer, so.company),
				"paid_amount": refund_amount,
				"received_amount": refund_amount,
				"reference_no": kwargs.get("reference_no") or so.name,
				"reference_date": today(),
				"custom_refund_against_sales_order": so.name,
				"custom_remarks": 1,
				"remarks": REFUND_REMARK.format(so.name),
			}
		)
		pe.setup_party_account_field()
		pe.set_missing_values()
		pe.flags.ignore_permissions = True
		pe.insert()
		pe.submit()

		response = {
			"status": "success",
			"message": _("Refund of {0} paid to {1}").format(refund_amount, so.customer),
			"sales_order": so.name,
			"customer": so.customer,
			"payment_entry": pe.name,
			"refund_amount": refund_amount,
			"paid_on_order": paid_on_order,
			"deduction": refundable - refund_amount,
		}
		api_log(api="Refund Sales Order", data=kwargs, response=response, status="Success")
		return response

	except Exception as e:
		return _fail("Refund Sales Order", kwargs, e)


# ---------------------------------------------------------------------------
# Additional payments against an existing Sales Order
# ---------------------------------------------------------------------------


def _order_paid_amount(so_name):
	"""Submitted advance payments allocated to the order (locking read, see _refunded_on_order)."""
	result = frappe.db.sql(
		"""
		select sum(r.allocated_amount)
		from `tabPayment Entry Reference` r
		join `tabPayment Entry` pe on pe.name = r.parent
		where pe.docstatus = 1
			and pe.payment_type = 'Receive'
			and r.reference_doctype = 'Sales Order'
			and r.reference_name = %s
		for update
		""",
		so_name,
	)
	return flt(result[0][0])


@frappe.whitelist(allow_guest=False)
def add_sales_order_payment(**kwargs):
	"""Record a later (part) payment against an existing, submitted Sales Order.

	Use when the customer pays again after the order was created, with or without an
	advance at creation. Each payments[] line becomes its own Payment Entry, so one call
	can take a split payment (e.g. 300 cash + 200 card).

	Identify the order with one of: sales_order, guid, customerOrdernumber.
	Other parameters:
		payments     (list, required) - [{"AMOUNT", "CARDTYPE" (mode of payment), "CARDNO"}]
		payment_guid (str, optional)  - unique id of this POS payment; a retry with the same
		                                id returns the original entries instead of paying twice
		posting_date (str, optional)  - default: today

	The total of all payments on the order may not exceed its grand total. The order must
	be submitted, not closed and not fully billed (pay a billed order against its invoice).
	All lines are created in one transaction: if any fails, none is saved.
	"""
	kwargs = _parse_payload(kwargs)
	try:
		frappe.db.savepoint(SAVEPOINT)
		so_name = _find_sales_order(kwargs)
		# Held until commit: concurrent payments for the same order run one after another,
		# so the balance check below always sees the earlier ones.
		_lock("Sales Order", so_name)
		so = frappe.get_doc("Sales Order", so_name)

		payment_guid = kwargs.get("payment_guid")
		if payment_guid:
			existing = frappe.get_all(
				"Payment Entry",
				filters={"custom_guid": payment_guid, "docstatus": 1},
				fields=["name", "paid_amount", "mode_of_payment", "reference_no"],
				order_by="creation asc",
			)
			if existing:
				return {
					"status": "success",
					"message": _("Payment {0} already recorded").format(payment_guid),
					"duplicate": True,
					"sales_order": so.name,
					"payments": existing,
					**_order_balance(so),
				}

		if so.docstatus != 1:
			raise frappe.ValidationError(_("Sales Order {0} is not submitted").format(so.name))
		if so.status in ("Closed", "Completed", "On Hold"):
			raise frappe.ValidationError(
				_("Sales Order {0} is {1}; payments are not allowed").format(so.name, so.status)
			)
		if flt(so.per_billed) >= 100:
			raise frappe.ValidationError(
				_("Sales Order {0} is fully billed; record the payment against its invoice").format(so.name)
			)

		lines = kwargs.get("payments") or []
		if isinstance(lines, str):
			lines = frappe.parse_json(lines)
		if not lines:
			raise frappe.ValidationError(_("payments is required"))
		for line in lines:
			if flt(line.get("AMOUNT")) <= 0:
				raise frappe.ValidationError(_("Each payment AMOUNT must be greater than 0"))
			mode = line.get("CARDTYPE")
			if mode and not frappe.db.exists("Mode of Payment", mode):
				raise frappe.DoesNotExistError(_("Mode of Payment {0} not found").format(mode))

		requested = sum(flt(line.get("AMOUNT")) for line in lines)
		already_paid = _order_paid_amount(so.name)
		balance = flt(so.grand_total) - already_paid
		if flt(requested, 2) > flt(balance, 2):
			raise frappe.ValidationError(
				_("Payment {0} exceeds the balance {1} of Sales Order {2} (total {3}, already paid {4})").format(
					requested, balance, so.name, so.grand_total, already_paid
				)
			)

		posting_date = _date_part(kwargs.get("posting_date")) or today()
		created = [
			_create_order_payment(
				so,
				flt(line.get("AMOUNT")),
				mode_of_payment=line.get("CARDTYPE"),
				reference_no=line.get("CARDNO"),
				guid=payment_guid,
				posting_date=posting_date,
			)
			for line in lines
		]

		so.reload()
		response = {
			"status": "success",
			"message": _("{0} received against Sales Order {1}").format(requested, so.name),
			"duplicate": False,
			"sales_order": so.name,
			"payments": [
				{
					"name": pe.name,
					"paid_amount": pe.paid_amount,
					"mode_of_payment": pe.mode_of_payment,
					"reference_no": pe.reference_no,
				}
				for pe in created
			],
			**_order_balance(so),
		}
		api_log(api="Add Sales Order Payment", data=kwargs, response=response, status="Success")
		return response

	except Exception as e:
		return _fail("Add Sales Order Payment", kwargs, e)


def _order_balance(so):
	paid = _order_paid_amount(so.name)
	return {
		"grand_total": flt(so.grand_total),
		"paid_amount": paid,
		"outstanding_amount": flt(so.grand_total) - paid,
	}


# ---------------------------------------------------------------------------
# Invoice Closing
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=False)
def create_invoice_closing(**kwargs):
	"""Create Invoice Closing for a POS Profile and date.

	Required parameters:
		pos_profile  (str)  - POS Profile name
		invoice_date (str)  - Date in YYYY-MM-DD format
		is_submit    (0/1)  - 0: create Invoice Closing only
		                      1: create + stock entries + submit Invoice Closing + submit Sales Invoices
	"""
	kwargs = _parse_payload(kwargs)
	try:
		frappe.db.savepoint(SAVEPOINT)
		pos_profile = kwargs.get("pos_profile")
		invoice_date = kwargs.get("invoice_date")
		is_submit = _as_bool(kwargs.get("is_submit"))

		if not pos_profile:
			raise frappe.ValidationError(_("pos_profile is required"))

		# Look for an existing non-cancelled Invoice Closing for this POS Profile + date
		existing_filters = {"pos_profile": pos_profile, "docstatus": ["!=", 2]}
		if invoice_date:
			existing_filters["invoice_date"] = invoice_date
		existing_name = frappe.db.exists("Invoice Closing", existing_filters)

		if existing_name:
			doc = frappe.get_doc("Invoice Closing", existing_name)
			if is_submit and doc.docstatus == 0:
				doc.submit()
				doc.reload()
				response = {"status": "submitted", "invoice_closing": _ic_summary(doc)}
				api_log(api="Create Invoice Closing", data=kwargs, response=str(response), status="Success")
				return response
			# Already submitted or is_submit=0 — just return it
			return {
				"status": "exists",
				"message": _("Invoice Closing already exists for this POS Profile and date"),
				"invoice_closing": _ic_summary(doc),
			}

		# insert() skips validate for a new doc; save() then runs validate, which
		# populates invoices, bom_items and raw_materials via the _load_* methods
		doc = frappe.new_doc("Invoice Closing")
		doc.pos_profile = pos_profile
		doc.invoice_date = invoice_date
		doc.date = invoice_date
		doc.warehouse = frappe.db.get_value("POS Profile", pos_profile, "warehouse")
		doc.insert(ignore_permissions=True)
		doc.save(ignore_permissions=True)
		doc.reload()

		if is_submit:
			# on_submit: create stock entries -> submit stock entries -> submit sales invoices
			doc.submit()
			doc.reload()

		response = {"status": "success", "invoice_closing": _ic_summary(doc)}
		api_log(api="Create Invoice Closing", data=kwargs, response=str(response), status="Success")
		return response

	except Exception as e:
		return _fail("Create Invoice Closing", kwargs, e, title="Create Invoice Closing Sync Failed")


def _ic_summary(doc):
	return {
		"name": doc.name,
		"docstatus": doc.docstatus,
		"status": doc.status,
		"pos_profile": doc.pos_profile,
		"warehouse": doc.warehouse,
		"invoice_date": doc.invoice_date,
		"total_amount": doc.total_amount,
		"invoice_count": len(doc.invoices),
		"bom_count": len(doc.bom_items),
		"raw_material_count": len(doc.raw_materials),
	}
