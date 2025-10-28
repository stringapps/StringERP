from frappe import _

def get_data():
	return {
		"fieldname": "custom_invoice_closing",
        "non_standard_fieldnames": {
        },
        "internal_links": {
			"Stock Entry": "custom_invoice_closing",
			"Sales Invoice": ["invoices", "invoice_no"],
		},
        "external_links": {
        },
		"transactions": [
			{"label": "Transactions", "items": ["Stock Entry"]},
			{"label": "Sales Invoice", "items": ["Sales Invoice"]},
		],
	}