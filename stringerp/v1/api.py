
import frappe
from frappe import _
from frappe.utils import flt


@frappe.whitelist(allow_guest=True)
def create_siv(**kwargs):
    try:
        # If API sends JSON body as string, ensure it's parsed
        if isinstance(kwargs, str):
            kwargs = frappe.parse_json(kwargs)
        elif isinstance(kwargs.get("data"), str):
            kwargs = frappe.parse_json(kwargs.get("data"))

        mapped_data = map_external_to_sales_invoice(kwargs)

        if not mapped_data.get("customer"):
            frappe.throw(_("Customer code is missing"))

        # Update existing invoice if same customer_order_no exists
        existing_invoice = frappe.db.exists(
            "Sales Invoice", {"customer_order_no": mapped_data.get("customer_order_no")})

        if existing_invoice:
            doc = frappe.get_doc("Sales Invoice", existing_invoice)
            doc.update(mapped_data)
            doc.save()
            return {"status": "updated", "invoice": doc.name}

        # Create new Sales Invoice
        doc = frappe.new_doc("Sales Invoice")
        doc.update(mapped_data)

        # Handle payment entries if is_paid or payments exist
        if mapped_data.get("is_paid") or kwargs.get("payments"):
            for pay in kwargs.get("payments", []):
                mode_of_payment = pay.get("CARDTYPE", "Cash")
                amount = flt(pay.get("AMOUNT", 0))
                reference_no = pay.get("CARDNO", "")
                doc.append("payments", {
                    "mode_of_payment": mode_of_payment,
                    "amount": amount,
                    "reference_no": reference_no
                })
        doc.insert(ignore_permissions=True)

        return {"status": "created", "invoice": doc}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Invoice Sync Failed")
        frappe.throw(_("Invoice creation/update failed: {0}").format(str(e)))


def map_external_to_sales_invoice(external_data):
    """Map incoming external POS data to ERPNext Sales Invoice format"""

    mapped = {
        "customer": external_data.get("customercode"),
        "customer_name": external_data.get("firstName") or "",
        "posting_date": external_data.get("billDate", "")[:10],
        "due_date": external_data.get("billDate", "")[:10],
        "remarks": external_data.get("Remarks"),
        "customer_order_no": external_data.get("customerOrdernumber"),
        "items": [],
        "is_pos": 1,
        "is_return": 0,
        "set_posting_time": 1,
        "is_paid": bool(external_data.get("ispaid")),
        "net_total": flt(external_data.get("nettotal", 0)),
        "discount_amount": flt(external_data.get("SubTotalDiscount", 0)),
        "taxes_and_charges": None,
        "other_charges_calculation": flt(external_data.get("VATAmount", 0)),
        "grand_total": flt(external_data.get("nettotalwithVAT", 0)),
        "total": flt(external_data.get("subtotal", 0))
    }

    # Items mapping
    for item in external_data.get("items", []):
        mapped["items"].append({
            "item_code": item.get("barcode"),
            "qty": flt(item.get("quantity", 1)),
            "rate": flt(item.get("UnitPrice", 0)),
            "discount_percentage": flt(item.get("UnitDisc", 0)),
            "description": item.get("DiscTID")
        })

    return mapped


@frappe.whitelist(allow_guest=False)
def get_customer_details(customer_id):
    """
    API Endpoint to fetch customer details with proper error handling.
    Example URL: /api/method/my_app.api.custom_api.get_customer_details?customer_id=CUST-0001
    """

    try:
        # ✅ Validate input
        if not customer_id:
            frappe.local.response.http_status_code = 400
            return {"status": "error", "message": _("Missing required parameter: customer_id")}

        # ✅ Check if customer exists
        customer = frappe.get_doc("Customer", customer_id)
        if not customer:
            frappe.local.response.http_status_code = 404
            return {"status": "error", "message": _("Customer not found")}

        # ✅ Return formatted data
        data = {
            "name": customer.name,
            "customer_name": customer.customer_name,
            "customer_group": customer.customer_group,
            "territory": customer.territory,
            "mobile_no": customer.mobile_no,
            "email_id": customer.email_id
        }

        return {
            "status": "success",
            "message": _("Customer fetched successfully"),
            "data": data
        }

    except frappe.DoesNotExistError:
        frappe.local.response.http_status_code = 404
        return {"status": "error", "message": _("Customer not found")}

    except frappe.PermissionError:
        frappe.local.response.http_status_code = 403
        return {"status": "error", "message": _("You do not have permission to access this resource.")}

    except Exception as e:
        # ✅ Log the error for debugging
        frappe.log_error(message=frappe.get_traceback(),
                         title="API Error: get_customer_details")

        # ✅ Return safe error response
        frappe.local.response.http_status_code = 500
        return {
            "status": "error",
            "message": _("An unexpected error occurred. Please contact support."),
            "error": str(e) if frappe.conf.developer_mode else None
        }
