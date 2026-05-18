// Copyright (c) 2025, D-codE and contributors
// For license information, please see license.txt

frappe.ui.form.on("Invoice Closing", {
    refresh(frm) {
        frm.refresh_fields(["status", "invoices", "bom_items", "raw_materials"]);
    },
    pos_profile(frm) {
        if (frm.doc.pos_profile) {
            frm.set_value("invoices", []);
            frm.set_value("bom_items", []);
            frappe.call({
                method: "stringerp.stringerp.doctype.invoice_closing.invoice_closing.get_siv",
                args: {
                    pos_profile: frm.doc.pos_profile,
                    invoice_closing: frm.doc.name,
                    inv_posting_date: frm.doc.invoice_date
                },
                callback: function (r) {
                    let data = r.message.invoices;
                    let total_amount = 0;
                    data.forEach(d => {
                        frm.add_child("invoices", {
                            invoice_no: d.name,
                            date: d.posting_date,
                            amount: d.grand_total,
                            bill_type: d.custom_bill_type
                        });
                        total_amount += d.grand_total;
                    });
                    frm.set_value("total_amount", total_amount);
                    let bom_items = r.message.bom_items;
                    bom_items.forEach(b => {
                        frm.add_child("bom_items", {
                            item_code: b.item_code,
                            invoice_name: b.parent,
                            qty: b.qty,
                            bom: b.bom
                        });
                    });
                    frm.refresh_field("bom_items");
                    frm.refresh_field("invoices");
                }
            });
        }
    },
    on_cancel(frm) {
        frm.refresh_field("status");
    },
    invoice_date(frm) {
        frm.set_value("pos_profile", "");
        frm.set_value("invoices", []);
        frm.set_value("bom_items", []);
        frm.set_value("total_amount", 0);
        frm.set_value("raw_materials", []);
    }
});

