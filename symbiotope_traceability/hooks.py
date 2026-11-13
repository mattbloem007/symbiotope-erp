app_name = "symbiotope_traceability"
app_title = "Symbiotope Traceability"
app_publisher = "Hakman Consultancy B.V."
app_description = "Botanical production traceability: harvest, processing, extraction, and inventory automation for Symbiotope."
app_email = "orders@symbiotope.com"
app_license = "Proprietary"

# Custom fields for the storefront <-> ERP integration (Milestone 5). Runs after
# every migrate, so the fields exist on any site the app is installed on.
after_migrate = [
    "symbiotope_traceability.storefront.setup_custom_fields",
    "symbiotope_traceability.purchasing.setup_purchasing_fields",
    "symbiotope_traceability.purchasing.setup_purchasing_permissions",
    "symbiotope_traceability.purchasing.setup_purchase_order_print_format",
    "symbiotope_traceability.user_mail.setup_user_mail_fields",
    "symbiotope_traceability.sop_forms.setup_sop_forms",
]

# Automation for Milestone 4's harvest -> processing -> extraction -> inventory loop.
# Ships as real hooks.py doc_events (rather than Server Scripts, which Frappe Cloud
# disables on shared/public bench groups as of v15) so it runs without a private bench.
doc_events = {
    "Processing Stage Log": {
        "after_insert": "symbiotope_traceability.automation.on_processing_stage_log_created",
    },
    "Extraction Batch": {
        "before_save": "symbiotope_traceability.automation.on_extraction_batch_before_save",
    },
    # A client's upload is drafted into an extraction form immediately, so it is
    # waiting for an admin's review rather than for someone to remember it.
    "SOP": {
        "before_save": "symbiotope_traceability.sop_forms.on_sop_before_save",
        "after_insert": "symbiotope_traceability.sop_forms.on_sop_created",
    },
    # The bought lane: a received plant-material line becomes a Quarantine lot.
    # Runs for receipts raised here and for ones typed directly into Desk.
    "Purchase Receipt": {
        "before_submit": "symbiotope_traceability.purchasing.stamp_intake_lots",
    },
}

# Frappe sends a new user's welcome mail (and password resets) as the *logged-in*
# user, so an admin's personal address becomes the From and the mail fails DMARC
# at the recipient while the queue reports it as sent. This override sends those
# two mails as the site instead, with the inviter as Reply-To.
override_doctype_class = {
    "User": "symbiotope_traceability.user_mail.SymbiotopeUser",
}
