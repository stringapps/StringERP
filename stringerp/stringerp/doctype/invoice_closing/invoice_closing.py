# Copyright (c) 2025, D-codE and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt
from erpnext.stock.utils import get_or_make_bin, get_stock_balance
import datetime


class InvoiceClosing(Document):
    def validate(self):
        """Refresh invoices, BOM items, and raw materials from DB on every save."""
        if self.docstatus != 0 or not self.pos_profile:
            return
        self._load_invoices_and_bom()
        self._load_raw_materials()

    def _load_invoices_and_bom(self):
        """Populate invoices and bom_items child tables by querying available draft invoices."""
        # Exclude current document's invoices only if no stock entry is linked to it yet
        exclude_current = not frappe.db.exists(
            "Stock Entry",
            {"custom_invoice_closing": self.name, "docstatus": ["!=", 2]}
        )
        if exclude_current:
            prev_used_inv = frappe.db.sql_list("""
                SELECT DISTINCT ICT.invoice_no
                FROM `tabInvoice Closing Table` ICT
                INNER JOIN `tabInvoice Closing` IC ON ICT.parent = IC.name
                WHERE IC.docstatus != 2 AND IC.name != %s
            """, (self.name,))
        else:
            prev_used_inv = frappe.db.sql_list("""
                SELECT DISTINCT ICT.invoice_no
                FROM `tabInvoice Closing Table` ICT
                INNER JOIN `tabInvoice Closing` IC ON ICT.parent = IC.name
                WHERE IC.docstatus != 2
            """)

        inv_filter = {"pos_profile": self.pos_profile, "docstatus": 0}
        if prev_used_inv:
            inv_filter["name"] = ["not in", prev_used_inv]
        if self.invoice_date:
            inv_filter["posting_date"] = ["=", self.invoice_date]

        invoices = frappe.get_all(
            "Sales Invoice",
            filters=inv_filter,
            fields=["name", "customer", "posting_date", "grand_total", "custom_bill_type"],
            order_by="posting_date desc"
        )
        if not invoices:
            return

        try:
            bom_items = get_bom_items(invoices, self.pos_profile)
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "Invoice Closing - BOM Load Error")
            frappe.throw(_("BOM load error during save: {0}").format(str(e)))

        self.invoices = []
        self.bom_items = []
        total_amount = 0
        for inv in invoices:
            self.append("invoices", {
                "invoice_no": inv.name,
                "date": inv.posting_date,
                "amount": inv.grand_total,
                "bill_type": inv.custom_bill_type
            })
            total_amount += inv.grand_total
        self.total_amount = total_amount

        for b in bom_items:
            self.append("bom_items", {
                "item_code": b.item_code,
                "invoice_name": b.parent,
                "qty": b.qty,
                "bom": b.bom
            })

    def _load_raw_materials(self):
        """Recalculate raw_materials child table from in-memory bom_items."""
        bom_totals = {}
        for b in self.bom_items:
            bom_totals[b.bom] = bom_totals.get(b.bom, 0) + b.qty

        raw_material_summary = {}
        for bom_name, total_qty in bom_totals.items():
            for item in frappe.get_all("BOM Item", filters={"parent": bom_name}, fields=["item_code", "qty"]):
                raw_material_summary[item.item_code] = (
                    raw_material_summary.get(item.item_code, 0) + item.qty * total_qty
                )

        warehouse = frappe.db.get_value("POS Profile", self.pos_profile, "warehouse")
        self.raw_materials = []
        for item_code, qty in raw_material_summary.items():
            self.append("raw_materials", {
                "item_code": item_code,
                "qty": qty,
                "stock_qty": get_stock_balance(item_code, warehouse)
            })

    @frappe.whitelist()
    def validate_raw_materials(self):
        """Recalculate and persist raw materials (kept for backward compatibility)."""
        if self.docstatus != 0:
            return
        bom_summary = get_bom_summary(self.name)
        raw_material_summary = {}
        for bom in bom_summary:
            for item in frappe.get_all("BOM Item", filters={"parent": bom.bom}, fields=["item_code", "qty"]):
                raw_material_summary[item.item_code] = (
                    raw_material_summary.get(item.item_code, 0) + item.qty * bom.total_qty
                )
        self.raw_materials = []
        warehouse = frappe.db.get_value("POS Profile", self.pos_profile, "warehouse")
        for item_code, qty in raw_material_summary.items():
            self.append("raw_materials", {
                "item_code": item_code,
                "qty": qty,
                "stock_qty": get_stock_balance(item_code, warehouse)
            })
        self.save(ignore_permissions=True)

    def on_submit(self):
        """On submit: create stock entries → submit stock entries → submit invoices."""
        # Step 1: Create stock entries from BOM synchronously (if any BOM items exist)
        if self.bom_items:
            create_stock_entry_from_bom_job(
                self.name, purpose="Manufacture", target_warehouse=self.warehouse, auto_submit=0
            )
            # Step 2: Submit the newly created stock entries
            self.submit_stock_entry()

        # Step 3: Submit all linked sales invoices
        for inv in self.invoices:
            frappe.get_doc("Sales Invoice", inv.invoice_no).submit()

        frappe.db.set_value("Invoice Closing", self.name, "status", "Invoice Submitted")
        frappe.msgprint(_("Invoices submitted successfully"))

    def submit_stock_entry(self):
        se_list = frappe.db.sql_list("""
            SELECT name FROM `tabStock Entry` WHERE custom_invoice_closing = %s AND docstatus = 0
        """, (self.name))
        if not se_list:
            # frappe.throw("No stock entry found!")
            return "Submitted"

        for se in se_list:
            try:
                frappe.get_doc("Stock Entry", se).submit()
            except Exception as e:
                frappe.throw(str(e))
        return "Stock Entry Submitted"

    @frappe.whitelist()    
    def cancel_invoice(self):
        for inv in self.invoices:
            cancel_and_amend_sales_invoice(inv.invoice_no)
        frappe.msgprint(_("Invoices cancelled"))
        frappe.db.set_value("Invoice Closing", self.name, "status", "Invoice Cancelled")
        
    @frappe.whitelist()
    def submit_invoice(self):
        for inv in self.invoices:
            frappe.get_doc("Sales Invoice", inv.invoice_no).submit()
        frappe.msgprint(_("Invoices submitted"))
        self.status = "Invoice Submitted"
        self.save(ignore_permissions = True)

    def on_cancel(self):
        self.cancel_invoice()

def get_bom_summary(invoice_closing):
    bom_summary = frappe.db.sql("""
        SELECT
            bom,
            SUM(qty) AS total_qty
        FROM
            `tabInvoice Closing BOM`
        WHERE
            parent = %s
        GROUP BY
            bom;
    """, (invoice_closing), as_dict=True)
    return bom_summary

# get sales invoices by pos profile =============================
@frappe.whitelist()
def get_siv(pos_profile, invoice_closing=None, inv_posting_date=None):
    if not pos_profile:
        frappe.throw("POS Profile is required")

    ic_doc = None
    additional_filter = ""
    if frappe.db.exists("Invoice Closing", invoice_closing):
        ic_doc = frappe.get_doc("Invoice Closing", invoice_closing)
        if not frappe.db.exists("Stock Entry", {"custom_invoice_closing": invoice_closing, "docstatus": ["!=", 2]}):
            additional_filter = " AND IC.name != '{0}'".format(invoice_closing)
    
    prev_used_inv = frappe.db.sql_list("""
        SELECT UNIQUE invoice_no
        FROM `tabInvoice Closing Table` ICT
        INNER JOIN `tabInvoice Closing` IC  
        ON ICT.parent = IC.name
        WHERE IC.docstatus != 2
        {additional_filter}
    """.format(additional_filter=additional_filter))
    
    inv_filter = filters={
            "pos_profile": pos_profile,
            "docstatus": 0  # only draft invoices
        }
    if prev_used_inv:
        inv_filter["name"] = ["not in", prev_used_inv]

    if inv_posting_date:
        inv_filter["posting_date"] = ["=", datetime.datetime.strptime(inv_posting_date, "%Y-%m-%d")]

    invoices = frappe.get_all(
        "Sales Invoice",
        filters=inv_filter,
        fields=[
            "name",
            "customer",
            "posting_date",
            "grand_total",
            "custom_bill_type"
        ],
        order_by="posting_date desc"
    )

    if not invoices:
        frappe.throw("No draft invoices found for this POS Profile")
    
    bom_items = get_bom_items(invoices, pos_profile)
    return {"invoices": invoices, "bom_items": bom_items}


def get_bom_items(invoices, pos_profile):
    warehouse = frappe.db.get_value("POS Profile", pos_profile, "warehouse")
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
            bom = frappe.db.get_list("BOM", 
                filters = {
                    "item": item.item_code,
                    "is_active": 1
                },
                or_filters = {
                    "custom_warehouse": warehouse,
                    "custom_bill_type": inv.custom_bill_type if inv.custom_bill_type else None,
                },
                fields = ["name"],
                limit = 1
            )
            if not bom:
                bom = frappe.db.get_list("BOM", 
                    filters = {
                        "item": item.item_code,
                        "is_active": 1,
                        "is_default": 1
                    },
                    fields = ["name"],
                    limit = 1
                )
            if not bom:
                frappe.throw(f"BOM not found for {item.item_code} (Invoice: {inv.name})")
            item["bom"] = bom[0].name
            
        bom_items.extend(items)

    return bom_items


# ============================================================


@frappe.whitelist()
def make_stock_entry_from_bom(
        invoice_closing,
        purpose="Manufacture",
        target_warehouse=None,
        auto_submit=0
):
    """
    Enqueue background job to create Stock Entry from BOM
    """
    frappe.enqueue(
        "stringerp.stringerp.doctype.invoice_closing.invoice_closing.create_stock_entry_from_bom_job",
        invoice_closing=invoice_closing,
        purpose=purpose,
        target_warehouse=target_warehouse,
        auto_submit=auto_submit,
        now=False,  # runs in background
        queue="long"  # use 'long' queue for heavy jobs
    )

    ic_doc = frappe.get_doc("Invoice Closing", invoice_closing)
    ic_doc.reload()

    return {
        "status": "queued",
        "message": _("Stock Entry creation for Invoice Closing {0} has been queued.").format(invoice_closing)
    }


def create_stock_entry_from_bom_job(
        invoice_closing,
        purpose,
        target_warehouse=None,
        auto_submit=0
):
    """
    Actual background job that creates the Stock Entry.
    """
    frappe.logger("stock_entry_from_bom").info(
        f"Creating Stock Entry from Invoice Closing {invoice_closing}")
    ic_doc = frappe.get_doc("Invoice Closing", invoice_closing)
    bom_summary = get_bom_summary(invoice_closing)

    if not bom_summary:
        # frappe.throw("No BOM found for this Invoice Closing")
        return
    
    for bom in bom_summary:
        if bom.total_qty==0:
            frappe.log_error(f"BOM {bom.bom} has zero total quantity for invoice closing {invoice_closing}", "Zero Quantity BOM - {0}".format(invoice_closing))
            continue
        if frappe.db.exists("Stock Entry", {"bom_no": bom.bom, "custom_invoice_closing": invoice_closing, "docstatus": ["!=", 2]}):
            frappe.log_error(f"Stock Entry already exists for BOM {bom.bom} against invoice closing {invoice_closing}", "Stock Entry Duplication - {0}".format(invoice_closing))
            continue
        bom_doc = frappe.get_doc("BOM", bom.bom)
    
        stock_entry = frappe.new_doc("Stock Entry")
        stock_entry.purpose = purpose
        stock_entry.from_bom = 1
        stock_entry.bom_no = bom_doc.name
        stock_entry.use_multi_level_bom = 1
        
        stock_entry.fg_completed_qty = bom.total_qty or 1
        stock_entry.set_posting_time = True
        posting_date, posting_time = get_posting_time_from_invoice(invoice_closing)
        stock_entry.posting_date = posting_date
        stock_entry.posting_time = posting_time
        stock_entry.inspection_required = bom_doc.inspection_required
        stock_entry.custom_invoice_closing = invoice_closing

        # Define warehouses
        stock_entry.from_warehouse = target_warehouse or bom_doc.default_fg_warehouse
        stock_entry.to_warehouse = target_warehouse or bom.default_fg_warehouse

        # Populate BOM items
        # stock_entry.get_items(bom.total_qty, bom_doc.item)
        stock_entry.get_items()
        # Add FG if missing
        if not any(d.is_finished_item for d in stock_entry.items):
            stock_entry.append("items", {
                "item_code": bom.item,
                "qty": bom.total_qty,
                "t_warehouse": target_warehouse or bom.default_fg_warehouse,
                "is_finished_item": 1,
                "uom": frappe.db.get_value("Item", bom.item, "stock_uom"),
                "conversion_factor": 1,
                "allow_zero_valuation_rate": 1
            })

        stock_entry.set_stock_entry_type()

        # Save and submit (optional)
        stock_entry.insert()
        if auto_submit:
            stock_entry.submit()
            ic_doc.db_set("status", "Stock Entry Submitted")
        else:
            ic_doc.db_set("status", "Stock Entry Created")
        frappe.logger("stock_entry_from_bom").info(
            f"Stock Entry {stock_entry.name} created successfully.")

# =================================================

def get_posting_time_from_invoice(invoice_closing):
    d = frappe.db.sql(
        """
        SELECT SI.posting_date, SI.posting_time, SI.name
        FROM `tabSales Invoice` SI
        INNER JOIN `tabInvoice Closing Table` ICT 
        ON SI.name = ICT.invoice_no
        WHERE ICT.parent = %s
        ORDER BY SI.posting_date, posting_time ASC
        LIMIT 1
        """,
        (invoice_closing),
        as_dict=True
    )
    return d[0].posting_date, d[0].posting_time

@frappe.whitelist()
def cancel_and_amend_sales_invoice(invoice_name):
    try:
        # 1️⃣ Get the Sales Invoice document
        doc = frappe.get_doc("Sales Invoice", invoice_name)
        guid = doc.custom_guid
        order_number = doc.custom_customer_order_no
        # 2️⃣ Check if it's submitted
        if doc.docstatus != 1:
            return {"status": "error", "message": f"Invoice {invoice_name} is not submitted."}
        # 3️⃣ Cancel the Sales Invoice
        doc.flags.ignore_links = True
        doc.custom_guid = ""
        doc.custom_customer_order_no = ""
        doc.cancel()
        frappe.db.commit()
        frappe.logger("sales_invoice_amend").info(
            f"Invoice {invoice_name} cancelled successfully.")

        # 4️⃣ Create an amended copy (new draft)
        new_doc = frappe.copy_doc(doc)
        new_doc.amended_from = invoice_name
        new_doc.docstatus = 0  # Draft
        new_doc.posting_date = doc.posting_date  # Optional: update date
        new_doc.custom_guid = guid
        new_doc.custom_customer_order_no = order_number
        new_doc.payments = []
        for pm in doc.payments:
            new_doc.append("payments", {
                "mode_of_payment": pm.mode_of_payment,
                "amount": pm.amount,
                "account": pm.account,
                "type": pm.type,
                "base_amount": pm.base_amount
            })
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



@frappe.whitelist()
def create_sales_invoice():
    # Create a new Sales Invoice
	for i in range(200):  # Change range for multiple invoices
		inv = frappe.new_doc("Sales Invoice")

		# Basic info
		inv.customer = "Walk In Customer"
		inv.is_pos = 1
		inv.pos_profile = "Eby Mathew"
		inv.posting_date = frappe.utils.nowdate()
		inv.due_date = frappe.utils.nowdate()

		# Add one dummy item
		inv.append("items", {
			"item_code": "0002",
			"qty": 1,
			"rate": 100
		})
		inv.append("items", {
			"item_code": "0001",
			"qty": 1,
			"rate": 100
		})

		# Save and submit
		inv.insert(ignore_permissions=True)
    # inv.submit()
