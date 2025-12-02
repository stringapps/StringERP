app_name = "stringerp"
app_title = "StringERP"
app_publisher = "D-codE"
app_description = "App For StringERP"
app_email = "mailtodecode@gmail.com"
app_license = "agpl-3.0"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "stringerp",
# 		"logo": "/assets/stringerp/logo.png",
# 		"title": "StringERP",
# 		"route": "/stringerp",
# 		"has_permission": "stringerp.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/stringerp/css/stringerp.css"
# app_include_js = "/assets/stringerp/js/stringerp.js"

# include js, css files in header of web template
# web_include_css = "/assets/stringerp/css/stringerp.css"
# web_include_js = "/assets/stringerp/js/stringerp.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "stringerp/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "stringerp/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "stringerp.utils.jinja_methods",
# 	"filters": "stringerp.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "stringerp.install.before_install"
# after_install = "stringerp.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "stringerp.uninstall.before_uninstall"
# after_uninstall = "stringerp.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "stringerp.utils.before_app_install"
# after_app_install = "stringerp.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "stringerp.utils.before_app_uninstall"
# after_app_uninstall = "stringerp.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "stringerp.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Sales Invoice": {
		"autoname": "stringerp.doc_events.sales_invoice.autoname",
	}
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"stringerp.tasks.all"
# 	],
# 	"daily": [
# 		"stringerp.tasks.daily"
# 	],
# 	"hourly": [
# 		"stringerp.tasks.hourly"
# 	],
# 	"weekly": [
# 		"stringerp.tasks.weekly"
# 	],
# 	"monthly": [
# 		"stringerp.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "stringerp.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "stringerp.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "stringerp.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "stringerp.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["stringerp.utils.before_request"]
# after_request = ["stringerp.utils.after_request"]

# Job Events
# ----------
# before_job = ["stringerp.utils.before_job"]
# after_job = ["stringerp.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"stringerp.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [
            [
                "name",
                "in",
                [
                    "Sales Invoice-custom_walkin_customer_address",
                    "Sales Invoice-custom_column_break_u2ryo",
                    "Sales Invoice-custom_walkin_customer_phone",
                    "Sales Invoice-custom_walkin_customer_name",
                    "Sales Invoice-custom_walkin_customer_details",
                    "Sales Invoice-custom_order_remarks",
                    "Sales Invoice-custom_column_break_mba9s",
                    "Sales Invoice-custom_online_order_id",
                    "Sales Invoice-custom_no_of_pax",
                    "Sales Invoice-custom_table",
                    "Sales Invoice-custom_column_break_1iel6",
                    "Sales Invoice-custom_delivery_man",
                    "Sales Invoice-custom_guid",
                    "Sales Invoice-custom_bill_type",
                    "Sales Invoice-custom_section_break_bhjug",
                    "Sales Invoice Item-custom_discount_type",
                    "Sales Invoice Item-custom_bom_item",
                    "Item-custom_bom_item",
                    "BOM-custom_bill_type",
                    "Sales Invoice-custom_walkin_customer_alt_phone",
                    "BOM-custom_warehouse",
                    "Stock Entry-custom_invoice_closing",
                    "Sales Invoice-custom_custom_name",
                    "Sales Invoice-custom_customer_order_no",
                    "Sales Invoice-custom_table_no",
                    "Sales Order-custom_walkin_customer_address",
                    "Sales Order-custom_customer_order_no",
                    "Sales Order-custom_guid",
                    "Sales Person-custom_sales_person_code",
                    "Customer-custom_customer_code"
                ],
            ],
        ],   
    },
    {
     "doctype": "Property Setter",
        "filters": [
            [
                "name",
                "in",
                [
				"Sales Invoice-main-field_order",
               
                ],
            ],
        ],
    }
]