"""Tests for stringerp.v1.api.

Unit tests run on any site. The end-to-end tests need real master data and read it from
site config, skipping when it is not set:

	bench --site <site> set-config --parse stringerp_test '{
		"customer": "DAR JABHAN TRADING COMPANY",
		"customer_code": "PLTC0083",
		"item": "PM003",
		"mode_of_payment": "Cash",
		"mode_of_payment_2": "CARD - Van Sale-DRY",
		"company": "Mashriq Al Dahbiya Trading Company - DRY"
	}'
	bench --site <site> run-tests --module stringerp.v1.test_api

Every test runs in a transaction that is rolled back afterwards.
"""

import unittest

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
