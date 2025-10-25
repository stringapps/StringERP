frappe.listview_settings["Invoice Closing"] = {
    has_indicator_for_draft: 1,
    get_indicator: function (doc) {
		var colors = {
			"Stock Entry Submitted": "yellow",
			"Stock Entry Created": "gray",
			"Invoice Submitted": "green",
            "Invoice Cancelled": "red",
            "Submitted": "blue",
		};
		let status = doc.status;
        if(doc.status == null){
            status = "Submitted";
        }
		return [__(status), colors[status], "status,=," + doc.status];
	},
}