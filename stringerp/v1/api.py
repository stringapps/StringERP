
import frappe
from frappe import _
from frappe.utils import flt
from stringerp.v1.utils import api_log, bill_type_map

@frappe.whitelist(allow_guest=True)
def create_siv(**kwargs):
    try:
        # If API sends JSON body as string, ensure it's parsed
        if isinstance(kwargs, str):
            kwargs = frappe.parse_json(kwargs)
        elif isinstance(kwargs.get("data"), str):
            kwargs = frappe.parse_json(kwargs.get("data"))
        response = None
        order_type = ""
        if kwargs.get("ordertype") == "Invoice":
            response = create_invoice(kwargs)
            order_type = "Invoice"
        elif kwargs.get("ordertype") == "Order":
            response = create_sord(kwargs)
            order_type = "Order"
        else:
            raise Exception("Invalid ordertype")
        return response        
    except Exception as e:
        if order_type:
            frappe.log_error(frappe.get_traceback(), order_type + " " + "Sync Failed")
            api_log(api="Create Sales " + order_type, data=kwargs, response=str(response), status="Failed", error=e)
        else:
            frappe.log_error(frappe.get_traceback(), "Sync Failed")
            api_log(api="Create Sales", data=kwargs, response=str(response), status="Failed", error=e)
        response = {"status": "error", "message": str(e)}
        return response

def create_invoice(kwargs):
        if not frappe.db.exists("Customer", kwargs.get("firstName")):
            raise Exception(_("Customer {0} is missing".format(kwargs.get("firstName"))))

        mapped_data = map_external_to_sales_invoice(kwargs)

        if not mapped_data.get("customer"):
            frappe.throw(_("Customer code is missing"))

        if kwargs.get("Reupload"):
            existing_invoice = frappe.db.exists("Sales Invoice", {"custom_guid": kwargs.get("guid"), "docstatus": 0})
            if existing_invoice:
                doc = frappe.get_doc("Sales Invoice", existing_invoice)
                doc.update(mapped_data)
            elif not existing_invoice:
                raise Exception("Reupload: No draft invoice found with GUID: {0}".format(kwargs.get("guid")))
        elif frappe.db.exists("Sales Invoice", {"custom_guid": kwargs.get("guid")}):
            raise Exception("Invoice already exists with GUID: {0}".format(kwargs.get("guid")))
        else:
            doc = frappe.new_doc("Sales Invoice")
            doc.update(mapped_data)
        doc.set_taxes()
        # doc.set_missing_values()
        # doc.calculate_taxes_and_totals()
        doc.save(ignore_permissions=True)

        invoice = frappe.db.get_value("Sales Invoice", doc.name, "*")
        invoice.items = frappe.db.get_all("Sales Invoice Item", {"parent": doc.name}, "*")
        invoice.payments = frappe.db.get_all("Sales Invoice Payment", {"parent": doc.name}, "*")
        invoice.taxes = frappe.db.get_all("Sales Taxes and Charges", {"parent": doc.name}, "*")

        response = {
            "status": "success",
            "invoice": invoice
        }
        api_log(api="Create Sales Invoice", data=kwargs, response=str(response), status="Success")
        return response

def map_external_to_sales_invoice(external_data):
    """Map incoming external POS data to ERPNext Sales Invoice format"""
    mapped = {
        "customer": external_data.get("firstName"),
        "ignore_pricing_rule": 1,
        "custom_walkin_customer_name": external_data.get("custname"),
        "set_posting_time": 1,
        "posting_date": external_data.get("CRTime", "")[:10],
        "posting_time": external_data.get("CRTime", "")[11:],
        "due_date": external_data.get("billDate", "")[:10],
        "remarks": external_data.get("Remarks"),
        "custom_customer_order_no": external_data.get("customerOrdernumber"),
        "custom_custom_name": 1,
        "items": [],
        "payments": [],
        "is_pos": 1,
        "is_return": 0,
        "is_paid": bool(external_data.get("ispaid")),
        "net_total": flt(external_data.get("nettotal", 0)),
        # "taxes_and_charges": None,
        "other_charges_calculation": flt(external_data.get("VATAmount", 0)),
        "grand_total": flt(external_data.get("nettotalwithVAT", 0)),
        "total": flt(external_data.get("subtotal", 0)),
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
        "sales_team":[]
    }

    # Items mapping
    for item in external_data.get("items", []):
        mapped["items"].append({
            "item_code": item.get("barcode"),
            "qty": flt(item.get("quantity", 1)),
            "price_list_rate": flt(item.get("rate", 0)),
            "rate": flt(item.get("rate", 0))-flt(item.get("discrate", 0)),
            "description": item.get("DiscTID")
        })

    for item in external_data.get("deliveryCharges", []):
        mapped["items"].append({
            "item_code": item.get("barcode"),
            "qty": flt(item.get("quantity", 1)),
            "price_list_rate": flt(item.get("rate", 0)),
            "rate": flt(item.get("rate", 0))-flt(item.get("discrate", 0)),
            "description": item.get("DiscTID")
        })

    if external_data.get("salesmancode"):
        mapped["sales_team"].append({
            "sales_person": external_data.get("salesmancode"),
            "allocated_percentage": 100
        })


    # Handle payment entries if is_paid or payments exist
    if external_data.get("ispaid"):
        for pay in external_data.get("payments", []):
            mode_of_payment = pay.get("CARDTYPE", "Cash")
            amount = flt(pay.get("AMOUNT", 0))
            reference_no = pay.get("CARDNO", "")
            mapped["payments"].append({
                "mode_of_payment": mode_of_payment,
                "amount": amount,
                "reference_no": reference_no
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

def create_sord(kwargs):
    # If API sends JSON body as string, ensure it's parsed
    if isinstance(kwargs, str):
        kwargs = frappe.parse_json(kwargs)
    elif isinstance(kwargs.get("data"), str):
        kwargs = frappe.parse_json(kwargs.get("data"))

    if not frappe.db.exists("Customer", kwargs.get("firstName")):
        customer = create_customer(kwargs)
        
    mapped_data = map_external_to_sales_order(kwargs)

    doc = frappe.new_doc("Sales Order")
    doc.update(mapped_data)
    doc.insert(ignore_permissions=True)

    response = {"status": "success", "order": doc}
    api_log(api="Create Sales Invoice", data=kwargs, response=str(response), status="Success")
    return response

def map_external_to_sales_order(external_data):
    mapped = {
        "customer": external_data.get("firstName"),
        "transaction_date": external_data.get("billDate", "")[:10],
        "delivery_date": external_data.get("billDate", "")[:10],
        "po_no": external_data.get("customerOrdernumber"),
        "items": [],
        "payments": [],
        "discount_amount": flt(external_data.get("SubTotalDiscount", 0)),
        "advance_paid": flt(external_data.get("nettotalwithVAT", 0)),
        "custom_guid": external_data.get("guid") or ""
    }

    for item in external_data.get("items", []):
        mapped["items"].append({
            "item_code": item.get("barcode"),
            "qty": flt(item.get("quantity", 1)),
            "price_list_rate": flt(item.get("rate", 0)),
            "rate": flt(item.get("rate", 0))-flt(item.get("discrate", 0)),
            "description": item.get("DiscTID")
        })

    for item in external_data.get("deliveryCharges", []):
        mapped["items"].append({
            "item_code": item.get("barcode"),
            "qty": flt(item.get("quantity", 1)),
            "price_list_rate": flt(item.get("rate", 0)),
            "rate": flt(item.get("rate", 0))-flt(item.get("discrate", 0)),
            "description": item.get("DiscTID")
        })

    return mapped

def create_customer(kwargs):
    customer = frappe.new_doc("Customer")
    customer.customer_name = kwargs.get("firstName")
    customer.tax_category = "Standard VAT"
    customer.insert()
    return customer.name