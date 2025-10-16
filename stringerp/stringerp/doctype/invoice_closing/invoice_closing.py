# Copyright (c) 2025, D-codE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class InvoiceClosing(Document):
    pass

# get sales invoices by pos profile =============================


@frappe.whitelist()
def get_siv(pos_profile):
    if not pos_profile:
        frappe.throw("POS Profile is required")
    invoices = frappe.get_all(
        "Sales Invoice",
        filters={
            "pos_profile": pos_profile,
            "docstatus": 0  # only draft invoices
        },
        fields=[
            "name",
            "customer",
            "posting_date",
            "grand_total"
        ],
        order_by="posting_date desc"
    )
    bom_items = get_bom_items(invoices)
    return {"invoices": invoices, "bom_items": bom_items}


def get_bom_items(invoices):
    bom_items = []
    for inv in invoices:
        items = frappe.get_all(
            "Sales Invoice Item",
            filters={
                "parent": inv.name,
                "custom_bom_item": 1
            },
            fields=["parent", "item_code",
                    "item_name", "qty", "rate", "amount"]
        )
        # Fetch BOM for each item
        for item in items:
            bom = frappe.db.get_value(
                "BOM", {"item": item.item_code, "is_active": 1}, "name")
            item["bom"] = bom
        bom_items.extend(items)

    return bom_items

# ============================================================


@frappe.whitelist()
def make_stock_entry_from_bom(
        bom_no,
        purpose="Material Issue",
        qty=1,
        target_warehouse=None,
        company=None,
        project=None,
        auto_submit=False
):
    """
    Enqueue background job to create Stock Entry from BOM
    """
    frappe.enqueue(
        "stringerp.stringerp.doctype.invoice_closing.invoice_closing.create_stock_entry_from_bom_job",
        bom_no=bom_no,
        purpose=purpose,
        qty=qty,
        target_warehouse=target_warehouse,
        company=company,
        project=project,
        auto_submit=auto_submit,
        now=False,  # runs in background
        queue="long"  # use 'long' queue for heavy jobs
    )

    return {
        "status": "queued",
        "message": _("Stock Entry creation for BOM {0} has been queued.").format(bom_no)
    }


def create_stock_entry_from_bom_job(
        bom_no,
        purpose="Material Issue",
        qty=1,
        target_warehouse=None,
        company=None,
        project=None,
        auto_submit=False
):
    """
    Actual background job that creates the Stock Entry.
    """
    frappe.logger("stock_entry_from_bom").info(
        f"Creating Stock Entry from BOM {bom_no}")

    bom = frappe.get_doc("BOM", bom_no)
    if not bom:
        frappe.throw(_("BOM {0} not found").format(bom_no))

    stock_entry = frappe.new_doc("Stock Entry")
    stock_entry.purpose = purpose
    stock_entry.company = company or bom.company
    stock_entry.from_bom = 1
    stock_entry.bom_no = bom.name
    stock_entry.use_multi_level_bom = bom.with_operations
    stock_entry.fg_completed_qty = qty or 1
    stock_entry.inspection_required = bom.inspection_required
    stock_entry.project = project

    # Define warehouses
    stock_entry.to_warehouse = target_warehouse or bom.default_fg_warehouse

    # Populate BOM items
    stock_entry.get_items(qty, bom.item)

    # Add FG if missing
    if not any(d.is_finished_item for d in stock_entry.items):
        stock_entry.append("items", {
            "item_code": bom.item,
            "qty": qty,
            "t_warehouse": target_warehouse or bom.default_fg_warehouse,
            "is_finished_item": 1,
            "uom": frappe.db.get_value("Item", bom.item, "stock_uom"),
            "conversion_factor": 1
        })

    stock_entry.set_stock_entry_type()

    # Save and submit (optional)
    stock_entry.insert(ignore_permissions=True)
    if auto_submit:
        stock_entry.submit()

    frappe.logger("stock_entry_from_bom").info(
        f"Stock Entry {stock_entry.name} created successfully.")

    return stock_entry.name


# =================================================


@frappe.whitelist()
def cancel_and_amend_sales_invoice(invoice_name):
    try:
        # 1️⃣ Get the Sales Invoice document
        doc = frappe.get_doc("Sales Invoice", invoice_name)

        # 2️⃣ Check if it's submitted
        if doc.docstatus != 1:
            return {"status": "error", "message": f"Invoice {invoice_name} is not submitted."}

        # 3️⃣ Cancel the Sales Invoice
        doc.cancel()
        frappe.db.commit()
        frappe.logger("sales_invoice_amend").info(
            f"Invoice {invoice_name} cancelled successfully.")

        # 4️⃣ Create an amended copy (new draft)
        new_doc = frappe.copy_doc(doc)
        new_doc.amended_from = invoice_name
        new_doc.docstatus = 0  # Draft
        new_doc.posting_date = frappe.utils.nowdate()  # Optional: update date
        new_doc.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger("sales_invoice_amend").info(
            f"Amended invoice created: {new_doc.name}")

        return {
            "status": "success",
            "message": f"Invoice {invoice_name} cancelled and amended as {new_doc.name}",
            "new_invoice": new_doc.name
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(),
                         "cancel_and_amend_sales_invoice_error")
        return {"status": "error", "message": str(e)}
