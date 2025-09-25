import frappe
from frappe import _

@frappe.whitelist(allow_guest=True)
def hello_world(hello):
    frappe.local.response["home_page"] = f"Hello, {hello}!"


@frappe.whitelist(allow_guest=True)
def create_or_update_sales_invoice(invoice_data):

    # try:
        # mapped_data = map_external_to_sales_invoice(invoice_data)
        # data = frappe.parse_json(mapped_data)
        # data = mapped_data

        # if data.get("name") and frappe.db.exists("Sales Invoice", data["name"]):
        #     doc = frappe.get_doc("Sales Invoice", data["name"])
        #     doc.update(data)
        #     doc.save()
        #     return {"status": "updated", "invoice": doc.name}

        # doc = frappe.new_doc("Sales Invoice")
        # doc.update(data)
        # doc.insert(ignore_permissions=True)
        return {"status": "created"}

    # except Exception as e:
    #     pass
        # frappe.log_error(message=frappe.get_traceback(), title="Invoice Sync Failed")
        # frappe.throw(_("Invoice creation/update failed: {0}").format(str(e)))

def map_external_to_sales_invoice(external_data):
    mapped = {
        "customer": external_data.get("customercode"),
        "customer_name": external_data.get("firstName"),
        "posting_date": external_data.get("billDate", "")[:10],  
        "due_date": external_data.get("billDate", "")[:10],     
        "remarks": external_data.get("Remarks"),
        "items": [
            {
                "item_code": item.get("barcode"),
                "qty": item.get("quantity"),
                "rate": float(item.get("UnitPrice", 0)),
                "discount_percentage": float(item.get("UnitDisc", 0)),
                "description": item.get("DiscTID"),
            }
            for item in external_data.get("items", [])
        ],
        "total": external_data.get("subtotal"),
        "discount_amount": external_data.get("SubTotalDiscount"),
        "net_total": external_data.get("nettotal"),
        "taxes_and_charges": external_data.get("VATAmount"),
        "grand_total": external_data.get("nettotalwithVAT"),
        "is_paid": bool(external_data.get("ispaid")),
        "customer_order_no": external_data.get("customerOrdernumber"),
        "payments": [
            {
                "mode_of_payment": payment.get("CARDTYPE", "Cash"),
                "amount": payment.get("AMOUNT", 0),
                "reference_no": payment.get("CARDNO", ""),
                "pos_paycode": payment.get("POS_PAYCODE", None)
            }
            for payment in external_data.get("payments", [])
        ] if external_data.get("payments") else [],
    }
    return mapped
