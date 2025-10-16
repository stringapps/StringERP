// Copyright (c) 2025, D-codE and contributors
// For license information, please see license.txt

frappe.ui.form.on("Invoice Closing", {
    refresh(frm) {
        frm.add_custom_button(__('Create Stock Entry from BOM'), function () {
            frappe.call({
                method: "stringerp.stringerp.doctype.invoice_closing.invoice_closing.make_stock_entry_from_bom",
                args: {
                    bom_no: "BOM-0002-001",
                    purpose: "Manufacture",
                    qty: 4,
                    target_warehouse: "Finished Goods - DSNK",
                    auto_submit: 0
                },
                callback: function (r) {
                    frappe.msgprint(r.message);
                }
            });
        });
    },
    pos_profile(frm) {
        if (frm.doc.pos_profile) {
            frm.set_value("invoices", []);
            frm.set_value("bom_items", []);
            frappe.call({
                method: "stringerp.stringerp.doctype.invoice_closing.invoice_closing.get_siv",
                args: {
                    pos_profile: frm.doc.pos_profile
                },
                callback: function (r) {
                    debugger;
                    let data = r.message.invoices;
                    data.forEach(d => {
                        frm.add_child("invoices", {
                            invoice_no: d.name,
                            date: d.posting_date,
                            amount: d.grand_total,
                            bill_type: d.custom_bill_type
                        });
                    });
                    let bom_items = r.message.bom_items;
                    bom_items.forEach(b => {
                        frm.add_child("bom_items", {
                            item_code: b.item_code,
                            invoice_name: b.parent  ,
                            qty: b.qty,
                            bom: b.bom
                        });
                    });
                    frm.refresh_field("bom_items");
                    frm.refresh_field("invoices");
                }
            });
        }
    }
});
