import frappe

def autoname(doc, method=None):
	if doc.custom_customer_order_no and doc.custom_custom_name:
		doc.name = doc.custom_customer_order_no

def validate(doc, method=None):
	for row in doc.items:
		if row.custom_discount_rate:
			row.discount_amount = row.price_list_rate - row.custom_discount_rate
			row.base_rate = row.rate = row.price_list_rate - row.custom_discount_rate
			row.base_amount = row.amount = (row.price_list_rate - row.custom_discount_rate) * row.qty