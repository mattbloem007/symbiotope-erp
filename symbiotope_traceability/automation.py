"""
Automation for Milestone 4's harvest -> processing -> extraction -> inventory loop.

Both functions here were validated field-by-field against the live site first (via
direct REST API calls) before being encoded here, since ERPNext's Stock Entry has a
lot of implicit validation (valuation rate, serial/batch bundle settings, etc.) that
isn't obvious from the doctype schema alone.
"""

import frappe


# ERPNext v16 builds a Serial and Batch Bundle for any row that carries a
# batch_no, which then collides with the batch_no still set on the row
# ("Serial and Batch Bundle ... has already created"). Outward rows fail
# outright. use_serial_batch_fields keeps the plain batch_no path instead.


def on_processing_stage_log_created(doc, method):
    """When a Processing Stage Log reaches the final 'Dried' stage, create the
    Raw Material lot (a native Batch) and the Stock Entry that receives it into
    the Raw Materials warehouse -- the "ERP receives +2.7kg Raw Material Lot"
    step from the client's spec, done automatically rather than by hand."""
    if doc.stage != "Dried":
        return

    harvest_batch = frappe.get_doc("Harvest Batch", doc.harvest_batch)
    if harvest_batch.raw_material_batch:
        return  # already processed for this harvest batch

    plant_source = frappe.get_doc("Plant Source", harvest_batch.plant_source)
    item_code = plant_source.raw_material_item
    if not item_code:
        frappe.log_error(
            title="Symbiotope Traceability",
            message=f"Plant Source {plant_source.name} has no raw_material_item set; "
            f"skipped auto-creating a Raw Material batch for {harvest_batch.name}.",
        )
        return

    batch_id = f"{harvest_batch.name}-RM"
    if frappe.db.exists("Batch", batch_id):
        batch = frappe.get_doc("Batch", batch_id)
    else:
        batch = frappe.get_doc({
            "doctype": "Batch",
            "batch_id": batch_id,
            "item": item_code,
            "harvest_batch": harvest_batch.name,
            "lot_status": "Quarantine",
        })
        batch.insert(ignore_permissions=True)

    qty = doc.weight_out or 0
    if qty > 0:
        stock_entry = frappe.get_doc({
            "doctype": "Stock Entry",
            "stock_entry_type": "Material Receipt",
            "company": "Symbiotope",
            "items": [{
                "item_code": item_code,
                "qty": qty,
                "uom": doc.uom or "Kg",
                "t_warehouse": "Raw Materials - S",
                "batch_no": batch.name,
                "use_serial_batch_fields": 1,
                "allow_zero_valuation_rate": 1,
            }],
        })
        stock_entry.insert(ignore_permissions=True)
        stock_entry.submit()

    harvest_batch.raw_material_batch = batch.name
    harvest_batch.status = "Quarantine"
    harvest_batch.save(ignore_permissions=True)


def on_extraction_batch_before_save(doc, method):
    """When an Extraction Batch's status changes to Complete: block any input
    Batch that isn't Released (quarantine gating), consume the inputs from Raw
    Materials, and create the output Extract lot + the Stock Entry that receives
    it into Extract Storage -- all the automatic ERP-inventory steps from the
    client's spec, not manual entry."""
    if doc.status != "Complete" or not doc.has_value_changed("status"):
        return

    for row in doc.input_batches:
        lot_status = frappe.db.get_value("Batch", row.batch, "lot_status")
        if lot_status != "Released":
            frappe.throw(
                f"Batch {row.batch} is not Released (status: {lot_status}) "
                f"and cannot be used in an extraction."
            )

    issue_items = []
    for row in doc.input_batches:
        item_code = frappe.db.get_value("Batch", row.batch, "item")
        issue_items.append({
            "item_code": item_code,
            "qty": row.qty_used,
            "uom": row.uom or "Kg",
            "s_warehouse": "Raw Materials - S",
            "batch_no": row.batch,
            "use_serial_batch_fields": 1,
            "allow_zero_valuation_rate": 1,
        })
    issue_entry = frappe.get_doc({
        "doctype": "Stock Entry",
        "stock_entry_type": "Material Issue",
        "company": "Symbiotope",
        "items": issue_items,
    })
    issue_entry.insert(ignore_permissions=True)
    issue_entry.submit()

    extract_batch_id = f"{doc.name}-EXT"
    extract_batch = frappe.get_doc({
        "doctype": "Batch",
        "batch_id": extract_batch_id,
        "item": doc.output_extract_item,
        "extraction_batch": doc.name,
        "lot_status": "Quarantine",
    })
    extract_batch.insert(ignore_permissions=True)

    receipt_entry = frappe.get_doc({
        "doctype": "Stock Entry",
        "stock_entry_type": "Material Receipt",
        "company": "Symbiotope",
        "items": [{
            "item_code": doc.output_extract_item,
            "qty": doc.output_quantity,
            "uom": doc.output_uom or "Kg",
            "t_warehouse": "Extract Storage - S",
            "batch_no": extract_batch.name,
            "use_serial_batch_fields": 1,
            "allow_zero_valuation_rate": 1,
        }],
    })
    receipt_entry.insert(ignore_permissions=True)
    receipt_entry.submit()

    doc.output_extract_batch = extract_batch.name
    total_input = sum(row.qty_used for row in doc.input_batches)
    if total_input:
        doc.yield_percent = (doc.output_quantity / total_input) * 100
