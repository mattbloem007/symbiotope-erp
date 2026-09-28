app_name = "symbiotope_traceability"
app_title = "Symbiotope Traceability"
app_publisher = "Hakman Consultancy B.V."
app_description = "Botanical production traceability: harvest, processing, extraction, and inventory automation for Symbiotope."
app_email = "orders@symbiotope.com"
app_license = "Proprietary"

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
}
