"""Tests for stringerp.v1.api.

Unit tests run on any site. The end-to-end tests need real master data and read it from
site config, skipping when it is not set:

	bench --site <site> set-config --parse stringerp_test '{
		"customer": "DAR JABHAN TRADING COMPANY",
		"customer_code": "PLTC0083",
		"item": "PM003",
		"mode_of_payment": "Cash",
		"mode_of_payment_2": "CARD - Van Sale-DRY",
		"company": "Mashriq Al Dahbiya Trading Company - DRY",
		"pos_profile": "Mashriq Al Dahbiya Trading Company - DRY"
	}'
	bench --site <site> run-tests --module stringerp.v1.test_api

Every test runs in a transaction that is rolled back afterwards.
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, now, random_string

from stringerp.v1 import api


class TestHelpers(FrappeTestCase):
	def test_as_bool(self):
		for value in (True, 1, "1", "true", " Yes ", "on"):
			self.assertTrue(api._as_bool(value), value)
		for value in (False, 0, None, "0", "false", "", 2, []):
			self.assertFalse(api._as_bool(value), value)

	def test_date_and_time_parts_tolerate_none(self):
		self.assertEqual(api._date_part("2026-03-22 11:50:42"), "2026-03-22")
		self.assertEqual(api._time_part("2026-03-22 11:50:42"), "11:50:42")
		self.assertEqual(api._date_part(None), "")
		self.assertEqual(api._time_part(None), "")

	def test_map_item_discount_percentage(self):
		row = api._map_item({"barcode": "X", "quantity": 2, "rate": 10, "UnitPrice": 10, "UnitDisc": 2.5})
		self.assertEqual(row["item_code"], "X")
		self.assertEqual(row["qty"], 2)
		self.assertEqual(row["price_list_rate"], 10)
		self.assertAlmostEqual(row["discount_percentage"], 25)
		self.assertEqual(api._map_item({"UnitPrice": 0, "UnitDisc": 1})["discount_percentage"], 0)

	def test_map_items_includes_delivery_charges(self):
		rows = api._map_items({"items": [{"barcode": "A"}], "deliveryCharges": [{"barcode": "DEL"}]})
		self.assertEqual([r["item_code"] for r in rows], ["A", "DEL"])
		self.assertEqual(api._map_items({"items": None}), [])

	def test_concurrency_errors_detected(self):
		self.assertTrue(api._is_concurrency_error(frappe.QueryDeadlockError("1020 Record has changed")))
		self.assertTrue(api._is_concurrency_error(frappe.QueryTimeoutError("1205 Lock wait timeout")))
		self.assertFalse(api._is_concurrency_error(Exception("plain message")))
		self.assertFalse(api._is_concurrency_error(frappe.ValidationError()))

	def test_parse_payload(self):
		self.assertEqual(api._parse_payload({"data": '{"a": 1}'}), {"a": 1})
		self.assertEqual(api._parse_payload({"a": 1}), {"a": 1})

	def test_error_status(self):
		for exc, code in (
			(frappe.PermissionError(), 403),
			(frappe.DoesNotExistError(), 404),
			(frappe.ValidationError(), 417),
			(Exception(), 417),
		):
			api._set_error_status(exc)
			self.assertEqual(frappe.local.response.http_status_code, code)


class TestValidation(FrappeTestCase):
	def tearDown(self):
		frappe.local.response.http_status_code = None

	def test_invalid_ordertype(self):
		r = api.create_siv(ordertype="Nope")
		self.assertEqual(r["status"], "error")
		self.assertEqual(r["message"], "Invalid ordertype")
		self.assertEqual(frappe.local.response.http_status_code, 417)

	def test_invoice_without_customer_code(self):
		r = api.create_siv(ordertype="Invoice")
		self.assertEqual(r["message"], "Customer code is missing")

	def test_cancel_needs_identifier(self):
		r = api.cancel_sales_order()
		self.assertEqual(r["status"], "error")
		self.assertEqual(frappe.local.response.http_status_code, 417)

	def test_cancel_unknown_order_is_404(self):
		r = api.cancel_sales_order(sales_order="SO-DOES-NOT-EXIST")
		self.assertEqual(r["status"], "error")
		self.assertEqual(frappe.local.response.http_status_code, 404)

	def test_refund_unknown_guid_is_404(self):
		api.refund_sales_order(guid="does-not-exist", refund_amount=1)
		self.assertEqual(frappe.local.response.http_status_code, 404)

	def test_invoice_closing_needs_pos_profile(self):
		r = api.create_invoice_closing()
		self.assertEqual(r["message"], "pos_profile is required")


class TestSalesOrderFlow(FrappeTestCase):
	"""Create (full / partial / split payment) -> cancel -> refund."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cfg = frappe._dict(frappe.conf.get("stringerp_test") or {})
		if not cls.cfg.get("customer") or not cls.cfg.get("item"):
			raise unittest.SkipTest("stringerp_test site config not set")
		if cls.cfg.get("company"):
			frappe.defaults.set_user_default("company", cls.cfg.company)

	def tearDown(self):
		frappe.local.response.http_status_code = None

	def order(self, payments=None, ispaid=0):
		tag = random_string(10)
		return {
			"firstName": self.cfg.customer,
			"customercode": self.cfg.customer_code,
			"guid": f"TEST-{tag}",
			"customerOrdernumber": f"TEST-{tag}",
			"ordertype": "Order",
			"ispaid": ispaid,
			"CRTime": now()[:19],
			"posProfile": self.cfg.get("pos_profile"),
			"items": [{"barcode": self.cfg.item, "quantity": 1, "UnitPrice": 1000, "rate": 1000}],
			"payments": payments or [],
		}

	def create(self, **kw):
		r = api.create_siv(**self.order(**kw))
		self.assertEqual(r["status"], "success", r.get("message"))
		return r

	def test_no_payment(self):
		r = self.create()
		self.assertEqual(r["paid_amount"], 0)
		self.assertEqual(r["outstanding_amount"], flt(r["order"].grand_total))

	def test_order_warehouse_from_pos_profile(self):
		r = self.create()
		wh = frappe.db.get_value("POS Profile", self.cfg.pos_profile, "warehouse")
		self.assertEqual(r["order"].set_warehouse, wh)
		self.assertTrue(all(i.warehouse == wh for i in r["order"].items))

	def test_order_explicit_warehouse_wins(self):
		wh = frappe.db.get_value(
			"Warehouse", {"company": self.cfg.company, "is_group": 0, "name": ["!=", frappe.db.get_value("POS Profile", self.cfg.pos_profile, "warehouse")]}, "name"
		)
		kw = self.order()
		kw["warehouse"] = wh
		r = api.create_siv(**kw)
		self.assertEqual(r["status"], "success", r.get("message"))
		self.assertTrue(all(i.warehouse == wh for i in r["order"].items))

	def test_order_without_warehouse_fails(self):
		kw = self.order()
		kw.pop("posProfile")
		kw["erp_comp_name"] = self.cfg.company
		r = api.create_siv(**kw)
		self.assertEqual(r["status"], "error")
		self.assertIn("Warehouse is required", r["message"])

	def test_order_warehouse_wrong_company_fails(self):
		wh = frappe.db.get_value("Warehouse", {"company": ["!=", self.cfg.company], "is_group": 0}, "name")
		if not wh:
			self.skipTest("no warehouse in another company")
		kw = self.order()
		kw["warehouse"] = wh
		r = api.create_siv(**kw)
		self.assertEqual(r["status"], "error")
		self.assertIn("does not belong", r["message"])

	def test_full_payment_without_lines(self):
		r = self.create(ispaid=1)
		self.assertEqual(r["outstanding_amount"], 0)
		self.assertEqual(
			frappe.db.get_value("Sales Order", r["order"].name, "advance_paid"), r["paid_amount"]
		)

	def test_partial_payment(self):
		r = self.create(payments=[{"CARDTYPE": self.cfg.mode_of_payment, "AMOUNT": 500}])
		self.assertEqual(r["paid_amount"], 500)
		self.assertEqual(r["payment"]["mode_of_payment"], self.cfg.mode_of_payment)
		self.assertEqual(frappe.db.get_value("Sales Order", r["order"].name, "advance_paid"), 500)

	def test_split_payment(self):
		modes = (self.cfg.mode_of_payment, self.cfg.mode_of_payment_2 or self.cfg.mode_of_payment)
		r = self.create(
			payments=[{"CARDTYPE": modes[0], "AMOUNT": 300}, {"CARDTYPE": modes[1], "AMOUNT": 200}]
		)
		self.assertEqual(len(r["payment"]), 2)
		self.assertEqual(r["paid_amount"], 500)

	def test_overpayment_rolls_back(self):
		kw = self.order(payments=[{"CARDTYPE": self.cfg.mode_of_payment, "AMOUNT": 999999}])
		r = api.create_siv(**kw)
		self.assertEqual(r["status"], "error")
		self.assertEqual(frappe.local.response.http_status_code, 417)
		self.assertFalse(
			frappe.db.exists("Sales Order", {"custom_customer_order_no": kw["customerOrdernumber"]})
		)

	def test_duplicate_order_rejected(self):
		kw = self.order()
		self.assertEqual(api.create_siv(**kw)["status"], "success")
		r = api.create_siv(**kw)
		self.assertEqual(r["status"], "error")
		self.assertIn("already exists", r["message"])

	def test_order_customer_resolved_by_code(self):
		r = self.create()
		self.assertEqual(r["order"].customer, self.cfg.customer)

	def test_order_unknown_customer_code_creates_nothing(self):
		kw = dict(
			self.order(), customercode="NO-SUCH-CODE", firstName="Brand New Customer " + random_string(6)
		)
		r = api.create_siv(**kw)
		self.assertEqual(r["status"], "error")
		self.assertIn("NO-SUCH-CODE", r["message"])
		self.assertFalse(frappe.db.exists("Customer", kw["firstName"]))

	def test_order_unknown_salesman_code(self):
		r = api.create_siv(**dict(self.order(), salesmancode="NO-SUCH-SALESMAN"))
		self.assertEqual(r["status"], "error")
		self.assertIn("Sales Person NO-SUCH-SALESMAN is missing", r["message"])

	def test_add_payment_to_unpaid_order(self):
		so = self.create()["order"].name
		mop = self.cfg.mode_of_payment
		r = api.add_sales_order_payment(
			sales_order=so, payments=[{"CARDTYPE": mop, "AMOUNT": 400, "CARDNO": "TX1"}]
		)
		self.assertEqual(r["status"], "success", r.get("message"))
		self.assertEqual(r["paid_amount"], 400)
		self.assertEqual(r["outstanding_amount"], r["grand_total"] - 400)
		self.assertEqual(r["payments"][0]["reference_no"], "TX1")
		self.assertEqual(frappe.db.get_value("Sales Order", so, "advance_paid"), 400)

	def test_add_split_payment_after_advance(self):
		kw = self.order(payments=[{"CARDTYPE": self.cfg.mode_of_payment, "AMOUNT": 200}])
		total = api.create_siv(**kw)["order"].grand_total
		modes = (self.cfg.mode_of_payment, self.cfg.mode_of_payment_2 or self.cfg.mode_of_payment)
		r = api.add_sales_order_payment(
			guid=kw["guid"], payments=[{"CARDTYPE": modes[0], "AMOUNT": 300}, {"CARDTYPE": modes[1], "AMOUNT": total - 500}]
		)
		self.assertEqual(r["status"], "success", r.get("message"))
		self.assertEqual(len(r["payments"]), 2)
		self.assertEqual((r["paid_amount"], r["outstanding_amount"]), (total, 0))

	def test_add_payment_over_balance_rolls_back(self):
		order = self.create(payments=[{"CARDTYPE": self.cfg.mode_of_payment, "AMOUNT": 700}])["order"]
		so, balance = order.name, order.grand_total - 700
		r = api.add_sales_order_payment(
			sales_order=so,
			payments=[{"CARDTYPE": self.cfg.mode_of_payment, "AMOUNT": balance - 100}, {"AMOUNT": 200}],
		)
		self.assertEqual(r["status"], "error")
		self.assertIn(f"exceeds the balance {balance}", r["message"])
		self.assertEqual(frappe.db.get_value("Sales Order", so, "advance_paid"), 700)

	def test_add_payment_retry_is_idempotent(self):
		so = self.create()["order"].name
		kw = dict(sales_order=so, payment_guid="PAY-" + random_string(10), payments=[{"AMOUNT": 250}])
		first = api.add_sales_order_payment(**kw)
		self.assertEqual(first["status"], "success", first.get("message"))
		second = api.add_sales_order_payment(**kw)
		self.assertTrue(second["duplicate"])
		self.assertEqual(second["payments"][0]["name"], first["payments"][0]["name"])
		self.assertEqual(second["paid_amount"], 250)

	def test_add_payment_validation(self):
		so = self.create()["order"].name
		r = api.add_sales_order_payment(sales_order=so, payments=[])
		self.assertIn("payments is required", r["message"])
		r = api.add_sales_order_payment(sales_order=so, payments=[{"AMOUNT": 0}])
		self.assertIn("greater than 0", r["message"])
		r = api.add_sales_order_payment(sales_order=so, payments=[{"AMOUNT": 10, "CARDTYPE": "NO-SUCH-MODE"}])
		self.assertEqual(frappe.local.response.http_status_code, 404)
		self.assertEqual(api.cancel_sales_order(sales_order=so)["status"], "success")
		r = api.add_sales_order_payment(sales_order=so, payments=[{"AMOUNT": 10}])
		self.assertIn("is not submitted", r["message"])

	def test_cancel_and_refund(self):
		so = self.create(payments=[{"CARDTYPE": self.cfg.mode_of_payment, "AMOUNT": 500}])["order"].name

		# refund is refused while the order is still open
		self.assertEqual(api.refund_sales_order(sales_order=so, refund_amount=100)["status"], "error")

		r = api.cancel_sales_order(sales_order=so)
		self.assertEqual(r["status"], "success", r.get("message"))
		self.assertEqual(len(r["cancelled_payments"]), 1)
		self.assertEqual(frappe.db.get_value("Sales Order", so, "docstatus"), 2)
		pe = r["cancelled_payments"][0]["name"]
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 2)

		self.assertIn("already cancelled", api.cancel_sales_order(sales_order=so)["message"])

		r = api.refund_sales_order(
			sales_order=so, refund_amount=400, mode_of_payment=self.cfg.mode_of_payment
		)
		self.assertEqual(r["status"], "success", r.get("message"))
		self.assertEqual(r["deduction"], 100)
		refund = frappe.get_doc("Payment Entry", r["payment_entry"])
		self.assertEqual(
			(refund.payment_type, refund.party, refund.paid_amount), ("Pay", self.cfg.customer, 400)
		)
		self.assertEqual(refund.custom_refund_against_sales_order, so)

		r = api.refund_sales_order(sales_order=so, refund_amount=200)
		self.assertEqual(r["status"], "error")
		self.assertIn("exceeds refundable amount 100", r["message"])


class TestInvoiceFlow(TestSalesOrderFlow):
	"""Independent invoice, and Sales Order -> Sales Invoice linking via soName."""

	# don't re-run the inherited order tests
	for _name in [n for n in dir(TestSalesOrderFlow) if n.startswith("test_")]:
		locals()[_name] = None
	del _name

	def invoice(self, so_name=None, qty=1, payments=None):
		kw = dict(self.order(), ordertype="Invoice", billDate=now()[:19], ispaid=1 if payments else 0)
		kw["items"] = [{"barcode": self.cfg.item, "quantity": qty, "UnitPrice": 1000, "rate": 1000}]
		kw["payments"] = payments or []
		if so_name:
			kw["soName"] = so_name
		return kw

	def create_invoice(self, **kw):
		r = api.create_siv(**self.invoice(**kw))
		self.assertEqual(r["status"], "success", r.get("message"))
		return frappe.get_doc("Sales Invoice", r["invoice"].name)

	def test_independent_invoice(self):
		si = self.create_invoice()
		self.assertTrue(all(not i.sales_order and not i.so_detail for i in si.items))
		self.assertFalse(si.advances)

	def test_invoice_linked_to_unpaid_order(self):
		so = self.create()["order"]
		si = self.create_invoice(so_name=so.name)
		self.assertEqual(si.items[0].sales_order, so.name)
		self.assertEqual(si.items[0].so_detail, so.items[0].name)
		self.assertFalse(si.advances)

	def test_invoice_pulls_partial_advance(self):
		so = self.create(payments=[{"CARDTYPE": self.cfg.mode_of_payment, "AMOUNT": 500}])["order"]
		si = self.create_invoice(so_name=so.name)
		self.assertEqual(len(si.advances), 1)
		self.assertEqual(si.advances[0].allocated_amount, 500)
		self.assertEqual(flt(si.total_advance), 500)
		self.assertEqual(flt(si.outstanding_amount), flt(si.grand_total) - 500)

	def submit_paying_balance(self, si):
		"""Pay the POS balance and submit. ZATCA signing is stubbed: the CLI isn't installed locally."""
		balance = flt(si.grand_total) - flt(si.total_advance)
		if balance > 0:
			si.append("payments", {"mode_of_payment": self.cfg.mode_of_payment, "amount": balance})
			si.save()
		with patch(
			"ksa_compliance.standard_doctypes.sales_invoice.create_sales_invoice_additional_fields_doctype"
		):
			si.submit()
		return si

	def test_invoice_pulls_full_advance_and_bills_order(self):
		so = self.create(ispaid=1)["order"]
		si = self.create_invoice(so_name=so.name)
		self.assertEqual(flt(si.total_advance), flt(so.grand_total))
		self.assertEqual(flt(si.outstanding_amount), flt(si.grand_total) - flt(so.grand_total))
		self.submit_paying_balance(si)
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 0)
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "per_billed"), 100)
		# a fully billed order can't be invoiced again, nor cancelled
		r = api.create_siv(**self.invoice(so_name=so.name))
		self.assertEqual(r["status"], "error")
		self.assertIn("fully billed", r["message"])
		self.assertIn("cancel these first", api.cancel_sales_order(sales_order=so.name)["message"])

	def test_partial_billing_of_order(self):
		kw = self.order(payments=[{"CARDTYPE": self.cfg.mode_of_payment, "AMOUNT": 500}])
		kw["items"][0]["quantity"] = 2
		so = api.create_siv(**kw)["order"]
		so_doc = frappe.get_doc("Sales Order", so.name)
		si = self.submit_paying_balance(self.create_invoice(so_name=so.name, qty=1))
		self.assertEqual(si.items[0].so_detail, so_doc.items[0].name)
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "per_billed"), 50)
		# second invoice bills the remaining qty; a third has nothing left to link
		si2 = self.create_invoice(so_name=so.name, qty=1)
		self.assertEqual(si2.items[0].so_detail, so_doc.items[0].name)
		r = api.create_siv(**self.invoice(so_name=so.name, qty=1))
		self.assertEqual(r["status"], "error")
		self.assertIn("No invoice item matches", r["message"])

	def test_partial_advance_plus_pos_balance(self):
		so = self.create(payments=[{"CARDTYPE": self.cfg.mode_of_payment, "AMOUNT": 500}])["order"]
		pe = frappe.db.get_value("Payment Entry Reference", {"reference_name": so.name}, "parent")
		si = self.submit_paying_balance(self.create_invoice(so_name=so.name))
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 0)
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "per_billed"), 100)
		self.assertEqual(
			frappe.db.get_value("Payment Entry Reference", {"parent": pe}, "reference_name"), si.name
		)

	def test_unknown_so_name(self):
		r = api.create_siv(**self.invoice(so_name="SO-DOES-NOT-EXIST"))
		self.assertEqual(r["status"], "error")
		self.assertEqual(frappe.local.response.http_status_code, 404)

	def test_cancelled_so_rejected(self):
		so = self.create()["order"].name
		api.cancel_sales_order(sales_order=so)
		r = api.create_siv(**self.invoice(so_name=so))
		self.assertEqual(r["status"], "error")
		self.assertIn("not submitted", r["message"])

	def test_item_not_on_order_rejected(self):
		so = self.create()["order"].name
		kw = self.invoice(so_name=so)
		other = frappe.db.get_value("Item", {"name": ["!=", self.cfg.item], "is_stock_item": 1, "disabled": 0}, "name")
		kw["items"] = [{"barcode": other, "quantity": 1, "UnitPrice": 10, "rate": 10}]
		r = api.create_siv(**kw)
		self.assertEqual(r["status"], "error")
		self.assertIn("No invoice item matches", r["message"])
