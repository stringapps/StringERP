import frappe

def autoname(doc, method=None):
    if doc.custom_customer_order_no and doc.custom_custom_name:
        doc.name = doc.custom_customer_order_no