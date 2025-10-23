import frappe
import json
from datetime import date, time
def api_log(**kwargs):
    doc = frappe.new_doc("API Call Log")
    doc.api = kwargs.get("api")
    doc.request_payload = str(kwargs.get("data"))
    doc.response = json.dumps(kwargs.get("response"))
    doc.error_details = kwargs.get("error")
    doc.date = date.today()
    doc.time = frappe.utils.now()
    doc.response_status = kwargs.get("status")
    doc.insert(ignore_permissions=True)

bill_type_map = {
    "DI": "Dine In",
    "HD": "Home Delivery",
    "TK": "Take Away",
    "ON": "Online",
    "OT": "Others"
}