import frappe

def autoname(doc, method=None):
	if doc.custom_customer_order_no and doc.custom_custom_name:
		doc.name = doc.custom_customer_order_no

def validate(doc, method=None):
	# for row in doc.items:
	# 	if row.custom_discount_rate:
	# 		row.discount_amount = row.custom_discount_rate
			# row.base_rate = row.rate = row.price_list_rate - row.custom_discount_rate
			# row.base_amount = row.amount = (row.price_list_rate - row.custom_discount_rate) * row.qty

	pass

def on_submit(doc, method=None):
	"""Reconcile Sales Order advances on POS invoices.

	ERPNext links advance Payment Entries to the invoice on submit only for non-POS invoices
	(SalesInvoice.on_submit skips update_against_document_in_jv when is_pos). Invoices created
	from a Sales Order through the POS API are POS invoices carrying the order's advances, so
	without this the advance stays unallocated and the invoice stays outstanding.
	"""
	if doc.is_pos and not doc.is_return and any(a.allocated_amount > 0 for a in doc.get("advances")):
		doc.update_against_document_in_jv()
