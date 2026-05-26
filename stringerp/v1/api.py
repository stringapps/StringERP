
import frappe
from frappe import _
from frappe.utils import flt
from stringerp.v1.utils import api_log, bill_type_map
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from datetime import date



def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False

@frappe.whitelist(allow_guest=False)
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
        elif kwargs.get("ordertype") == "Debit note":
            response = create_invoice(kwargs)
            order_type = "Debit note"
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
    if not kwargs.get("customercode"):
        raise Exception(_("Customer code is missing"))

    if not frappe.db.exists("Customer", {"custom_customer_code": kwargs.get("customercode")}):
        raise Exception(_("Customer with customer code {0} is missing".format(kwargs.get("customercode"))))

    # if not frappe.db.exists("Customer", kwargs.get("firstName")):
    #     raise Exception(_("Customer {0} is missing".format(kwargs.get("firstName"))))

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
    elif kwargs.get("guid") and frappe.db.exists("Sales Invoice", {"custom_guid": kwargs.get("guid")}):
        raise Exception("Invoice already exists with GUID: {0}".format(kwargs.get("guid")))
    elif kwargs.get("customerOrdernumber") and frappe.db.exists("Sales Invoice", {"custom_customer_order_no": kwargs.get("customerOrdernumber")}):
        raise Exception("Order already exists with Customer Order No: {0}".format(kwargs.get("customerOrdernumber")))
    else:
        doc = frappe.new_doc("Sales Invoice")
        doc.update(mapped_data)
    doc.set_taxes()
    # doc.set_missing_values()
    
    doc.calculate_taxes_and_totals()
    doc.update_stock = 1 
    
    if kwargs.get("ordertype") == "Debit note":
        doc.is_debit_note = 1
    
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
        # "customer": external_data.get("firstName"),
        # "company": external_data.get("erp_comp_name"),
        # "custom_invoice_type": external_data.get("zatca_type"),
        "customer": frappe.db.get_value("Customer", {"custom_customer_code": external_data.get("customercode")}, "name"),
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
        "sales_team":[],
        "custom_online_order_id": external_data.get("onlineOrderId")
    }

    # Items mapping
    for item in external_data.get("items", []):
        mapped["items"].append({
            "item_code": item.get("barcode"),
            "qty": flt(item.get("quantity", 1)),
            "price_list_rate": flt(item.get("rate", 0)),
            "discount_percentage": (flt(item.get("UnitDisc", 0)))/flt(item.get("UnitPrice", 0))*100 if flt(item.get("UnitPrice", 0)) else 0,
            "custom_discount_rate": flt(item.get("UnitDisc", 0)),
            "custom_discount_type": item.get("DiscTID")
        })

    for item in external_data.get("deliveryCharges", []):
        mapped["items"].append({
            "item_code": item.get("barcode"),
            "qty": flt(item.get("quantity", 1)),
            "price_list_rate": flt(item.get("rate", 0)),
            "discount_percentage": (flt(item.get("UnitDisc", 0)))/flt(item.get("UnitPrice", 0))*100 if flt(item.get("UnitPrice", 0)) else 0,
            "custom_discount_rate": flt(item.get("UnitDisc", 0)),
            "custom_discount_type": item.get("DiscTID")
        })

    if external_data.get("salesmancode"):
        sp = frappe.db.exists("Sales Person", {"custom_sales_person_code": external_data.get("salesmancode")})
        if not sp:
            raise Exception("Sales Person {0} is missing".format(external_data.get("salesmancode")))
        mapped["sales_team"].append({
            "sales_person": sp,
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
    if kwargs.get("guid") and frappe.db.exists("Sales Order", {"custom_guid": kwargs.get("guid")}):
        raise Exception("Order already exists with GUID: {0}".format(kwargs.get("guid")))
           
    if kwargs.get("customerOrdernumber") and frappe.db.exists("Sales Order", {"custom_customer_order_no": kwargs.get("customerOrdernumber")}):
        raise Exception("Order already exists with Customer Order No: {0}".format(kwargs.get("customerOrdernumber")))

    if not frappe.db.exists("Customer", kwargs.get("firstName")):
        customer = create_customer(kwargs)
        
    mapped_data = map_external_to_sales_order(kwargs)

    doc = frappe.new_doc("Sales Order")
    doc.update(mapped_data)
    doc.insert(ignore_permissions=True)
    doc.submit()

    payment = {}
    if kwargs.get("ispaid"):
        pe_doc = get_payment_entry(dt="Sales Order", dn=doc.name)
        pe_doc.reference_no = doc.name
        pe_doc.reference_date = doc.transaction_date
        pe_doc.insert()
        pe_doc.submit()
        payment = pe_doc.as_dict()

    response = {"status": "success", "order": doc, "payment": payment}
    api_log(api="Create Sales Order", data=kwargs, response=str(response), status="Success")
    return response

def map_external_to_sales_order(external_data):
    mapped = {
        "customer": external_data.get("firstName"),
        "custom_walkin_customer_address": external_data.get("address1") or "",
        "transaction_date": external_data.get("CRTime", "")[:10] or date.today(),
        "delivery_date": external_data.get("CRTime", "")[:10] or date.today(),
        "remarks": external_data.get("Remarks"),
        "custom_customer_order_no": external_data.get("customerOrdernumber"),
        "items": [],
        "payments": [],
        "discount_amount": flt(external_data.get("SubTotalDiscount", 0)),
        "custom_guid": external_data.get("guid") or "",
        "disable_rounded_total": True,
        "sales_team": []
    }

    for item in external_data.get("items", []):
        mapped["items"].append({
            "item_code": item.get("barcode"),
            "qty": flt(item.get("quantity", 1)),
            "price_list_rate": flt(item.get("rate", 0)),
            "rate": flt(item.get("rate", 0))-flt(item.get("discrate", 0)),
        })

    for item in external_data.get("deliveryCharges", []):
        mapped["items"].append({
            "item_code": item.get("barcode"),
            "qty": flt(item.get("quantity", 1)),
            "price_list_rate": flt(item.get("rate", 0)),
            "rate": flt(item.get("rate", 0))-flt(item.get("discrate", 0)),
        })

    if external_data.get("salesmancode"):
        mapped["sales_team"].append({
            "sales_person": external_data.get("salesmancode"),
            "allocated_percentage": 100
        })
        
    return mapped

def create_customer(kwargs):
    customer = frappe.new_doc("Customer")
    customer.customer_name = kwargs.get("firstName")
    customer.tax_category = "Standard VAT"
    customer.insert()
    return customer.name


@frappe.whitelist(allow_guest=False)
def create_invoice_closing(**kwargs):
    """
    Create Invoice Closing for a POS Profile and date.

    Required parameters:
        pos_profile  (str)  – POS Profile name
        invoice_date (str)  – Date in YYYY-MM-DD format
        is_submit    (0/1)  – 0: create Invoice Closing only
                              1: create + stock entries + submit Invoice Closing + submit Sales Invoices
    """
    try:
        if isinstance(kwargs, str):
            kwargs = frappe.parse_json(kwargs)
        elif isinstance(kwargs.get("data"), str):
            kwargs = frappe.parse_json(kwargs.get("data"))

        pos_profile  = kwargs.get("pos_profile")
        invoice_date = kwargs.get("invoice_date")
        is_submit    = _as_bool(kwargs.get("is_submit"))

        if not pos_profile:
            raise Exception(_("pos_profile is required"))

        # Look for an existing non-cancelled Invoice Closing for this POS Profile + date
        existing_filters = {"pos_profile": pos_profile, "docstatus": ["!=", 2]}
        if invoice_date:
            existing_filters["invoice_date"] = invoice_date
        existing_name = frappe.db.exists("Invoice Closing", existing_filters)

        if existing_name:
            doc = frappe.get_doc("Invoice Closing", existing_name)
            if is_submit and doc.docstatus == 0:
                # Submit the existing draft
                doc.submit()
                doc.reload()
                response = {
                    "status": "submitted",
                    "invoice_closing": _ic_summary(doc),
                }
                api_log(api="Create Invoice Closing", data=kwargs, response=str(response), status="Success")
                return response
            # Already submitted or is_submit=0 — just return it
            return {
                "status": "exists",
                "message": "Invoice Closing already exists for this POS Profile and date",
                "invoice_closing": _ic_summary(doc),
            }

        warehouse = frappe.db.get_value("POS Profile", pos_profile, "warehouse")

        # insert triggers is_new so validate skips; save() triggers validate which
        # auto-populates invoices, bom_items and raw_materials via _load_* methods
        doc = frappe.new_doc("Invoice Closing")
        doc.pos_profile  = pos_profile
        doc.invoice_date = invoice_date
        doc.date         = invoice_date
        doc.warehouse    = warehouse
        doc.insert(ignore_permissions=True)
        doc.save(ignore_permissions=True)
        doc.reload()

        if is_submit:
            # on_submit handles: create stock entries → submit stock entries → submit sales invoices
            doc.submit()
            doc.reload()
        response = {
            "status": "success",
            "invoice_closing": _ic_summary(doc),
        }

        api_log(api="Create Invoice Closing", data=kwargs, response=str(response), status="Success")
        return response

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Create Invoice Closing Sync Failed")
        api_log(api="Create Invoice Closing", data=kwargs, response="", status="Failed", error=e)
        return {"status": "error", "message": str(e)}


def _ic_summary(doc):
    return {
        "name":               doc.name,
        "docstatus":          doc.docstatus,
        "status":             doc.status,
        "pos_profile":        doc.pos_profile,
        "warehouse":          doc.warehouse,
        "invoice_date":       doc.invoice_date,
        "total_amount":       doc.total_amount,
        "invoice_count":      len(doc.invoices),
        "bom_count":          len(doc.bom_items),
        "raw_material_count": len(doc.raw_materials),
    }

