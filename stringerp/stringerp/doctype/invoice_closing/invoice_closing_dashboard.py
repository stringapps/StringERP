from frappe import _

def get_data():
	return {
		"fieldname": "custom_invoice_closing",
        "non_standard_fieldnames": {
        },
        "internal_links": {
			"Stock Entry": "custom_invoice_closing",
		},
        "external_links": {
        },
		"transactions": [
			{"label": "Transactions", "items": ["Stock Entry"]},
		],
	}