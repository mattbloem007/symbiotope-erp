"""Purchasing -> lot intake: the bought lane into the traceability loop.

Why this module exists
----------------------
The harvest lane starts with a Harvest Batch, which is a *picking event*: it
carries a harvest date, an amount harvested, and a harvester, and the automation
turns it into a Raw Material lot once it reaches Dried.

A bought delivery has none of that. Until now the only way to get supplier
herbs into the loop was to record a fictional harvest -- a delivery date in
"Harvest Date", a supplier's weight in "Amount Harvested" -- which made the
traceability record say something that never happened, and left the purchase
itself (cost, supplier, delivery) outside the system entirely.

This module records the purchase properly instead:

    Purchase Order (what was ordered from whom)
        -> Purchase Receipt (what actually arrived, at what cost)
            -> one Quarantine lot per received plant-material line
                -> the existing QA release gate -> extraction -> Extract lot

Nothing downstream changes. `automation.on_extraction_batch_before_save`
already consumes any Batch whose `lot_status` is Released, regardless of where
it came from, so a bought lot and a harvested lot meet at the same QA gate.

Why the writes live here rather than on Frappe's DocPerm path
------------------------------------------------------------
The client chose to reuse ERPNext's Stock Manager role rather than invent a
"Procurement" role. Stock Manager holds no custom DocPerm on Plant Source or
Batch (those doctypes have hand-set custom permissions that override ERPNext's
native rows), so it cannot create the lot a receipt needs. It also cannot
submit a Purchase Order or Purchase Receipt through the generic REST path in
every configuration. Rather than grant a stock role write access to
traceability records, the two write entry points below are gated on holding
the role and then run with `ignore_permissions` -- the same shape as
`storefront.ship_order` and `receive_finished_goods`.

Reads are deliberately system reads (`frappe.get_all`), for the reason spelled
out in api.py: the role that buys the material is not necessarily a role that
has DocPerm on the document. They are narrow and return only what the Console
renders. `setup_purchasing_permissions()` grants Stock Manager *read* access
to the traceability doctypes so the Console's existing screens (lots, plant
sources, dashboard) work for them too.
"""

import json
import re
from html import escape as escape_html

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import (
    add_days,
    flt,
    getdate,
    nowdate,
    nowtime,
    validate_email_address,
)

from symbiotope_traceability.automation import system_permissions

ADMIN_ROLES = frozenset({"Symbiotope Admin", "System Manager"})
#: Who may raise supplier orders and record deliveries. Deliberately the same
#: role ERPNext already gives purchasing/stock rights to.
PROCUREMENT_ROLES = ADMIN_ROLES | {"Stock Manager"}

PLANT_PARTS = (
    "Leaf",
    "Root",
    "Flower",
    "Stem",
    "Whole Plant",
    "Seed",
    "Bark",
    "Other",
)

RAW_MATERIALS_WAREHOUSE_HINT = (
    "Create a non-group warehouse for raw materials (for example "
    '"Raw Materials - {abbr}") before recording a delivery.'
)


# ---------------------------------------------------------------------------
# Setup (runs after every migrate, so the fields and read access always exist)
# ---------------------------------------------------------------------------


def setup_purchasing_fields():
    """Custom fields that carry plant material through a purchase.

    The lot itself (Batch) records where the quantity came from, because that
    is the record a recall starts from. The order and receipt rows carry the
    same links so a buyer can see them while the delivery is still open.
    """
    fields = {
        "Purchase Order Item": [
            {
                "fieldname": "plant_source",
                "fieldtype": "Link",
                "options": "Plant Source",
                "label": "Plant Source",
                "insert_after": "item_name",
                "description": "Which botanical source this line buys. Can also be set when the delivery arrives.",
            },
            {
                "fieldname": "plant_part",
                "fieldtype": "Select",
                "options": "\n".join(PLANT_PARTS),
                "label": "Plant Part",
                "insert_after": "plant_source",
            },
        ],
        "Purchase Receipt Item": [
            {
                "fieldname": "plant_source",
                "fieldtype": "Link",
                "options": "Plant Source",
                "label": "Plant Source",
                "insert_after": "item_name",
                "description": "The source this received quantity is logged against. A Quarantine lot is created from it on submit.",
            },
            {
                "fieldname": "plant_part",
                "fieldtype": "Select",
                "options": "\n".join(PLANT_PARTS),
                "label": "Plant Part",
                "insert_after": "plant_source",
            },
            {
                "fieldname": "supplier_batch_no",
                "fieldtype": "Data",
                "label": "Supplier Batch / Lot No.",
                "insert_after": "plant_part",
                "description": "The supplier's own lot number, so their paperwork can be matched to ours.",
            },
        ],
        "Batch": [
            {
                "fieldname": "plant_source",
                "fieldtype": "Link",
                "options": "Plant Source",
                "label": "Plant Source",
                "insert_after": "item",
                "search_index": 1,
                "description": "Where this lot came from -- the harvest or the supplier delivery.",
            },
            {
                "fieldname": "intake_type",
                "fieldtype": "Select",
                "options": "Harvested\nPurchased",
                "label": "Intake Type",
                "insert_after": "plant_source",
                "read_only": 1,
            },
            {
                "fieldname": "supplier",
                "fieldtype": "Link",
                "options": "Supplier",
                "label": "Supplier",
                "insert_after": "intake_type",
                "read_only": 1,
            },
            {
                "fieldname": "supplier_batch_no",
                "fieldtype": "Data",
                "label": "Supplier Batch / Lot No.",
                "insert_after": "supplier",
                "read_only": 1,
            },
            {
                "fieldname": "purchase_receipt",
                "fieldtype": "Link",
                "options": "Purchase Receipt",
                "label": "Purchase Receipt",
                "insert_after": "supplier_batch_no",
                "read_only": 1,
                "search_index": 1,
            },
        ],
        "Company": [
            {
                "fieldname": "symbiotope_purchasing_section",
                "fieldtype": "Section Break",
                "label": "Purchasing",
                "insert_after": "abbr",
            },
            {
                "fieldname": "purchase_order_reply_to",
                "fieldtype": "Data",
                "options": "Email",
                "label": "Purchase Order Reply-To",
                "insert_after": "symbiotope_purchasing_section",
                "description": (
                    "Where a supplier's reply to an emailed Purchase Order is "
                    "sent. Falls back to the Email field under Address & Contact, "
                    "then to the address of whoever sent the order."
                ),
            },
        ],
    }

    for doctype, specs in fields.items():
        # A field ERPNext already ships (Batch has a standard `supplier`, for
        # one) cannot be re-added as a custom field; use the standard one.
        wanted = [
            df for df in specs if not _is_standard_field(doctype, df["fieldname"])
        ]
        if wanted:
            create_custom_fields({doctype: wanted}, update=True)


def setup_purchasing_permissions():
    """Let Stock Manager *read* the traceability record.

    It buys the material, so it should be able to see where it went. Read only:
    every write this role needs goes through the gated methods below, so it
    never gains the ability to release a lot or run an extraction.
    """
    for doctype in ("Plant Source", "Harvest Batch", "Extraction Batch", "Batch"):
        if frappe.db.exists(
            "Custom DocPerm",
            {"parent": doctype, "role": "Stock Manager", "permlevel": 0},
        ):
            continue
        frappe.permissions.add_permission(doctype, "Stock Manager", 0, "read")


def _is_standard_field(doctype, fieldname):
    if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
        return False
    return frappe.get_meta(doctype).has_field(fieldname)


# ---------------------------------------------------------------------------
# The automation: a receipt row becomes a Quarantine lot
# ---------------------------------------------------------------------------


def stamp_intake_lots(doc, method=None):
    """`before_submit` on Purchase Receipt: give every plant-sourced row its
    own Quarantine lot.

    This is the bought equivalent of the Dried-stage automation. It runs for
    receipts created here *and* for ones raised by hand in Desk, so a buyer
    working in the ERP still produces traceable lots.

    Batches are created before the stock ledger is written, and `batch_no` is
    set on the row so the stock lands in that lot at the supplier's rate -- no
    zero-valuation shortcut, which is what made bought material invisible to
    inventory value before.
    """
    for row in doc.items:
        if not row.get("plant_source"):
            continue
        if row.get("batch_no"):
            # Already stamped (the Console method does it before submit so the
            # lot carries the operator as owner); do not create a second one.
            continue

        item = _stock_item(row.item_code)
        plant_source = _plant_source_doc(row.get("plant_source"))
        batch = _create_lot(plant_source, row, doc)
        row.batch_no = batch.name
        row.use_serial_batch_fields = 1
        _remember_item_on_source(plant_source, item.name)
        _remember_acquisition_date(plant_source, doc.posting_date)


def _create_lot(plant_source, row, receipt):
    item = _stock_item(row.item_code)
    batch = frappe.get_doc(
        {
            "doctype": "Batch",
            "batch_id": _next_lot_id(plant_source, receipt.posting_date),
            "item": item.name,
            "plant_source": plant_source.name,
            "intake_type": "Purchased",
            "supplier": receipt.supplier,
            "supplier_batch_no": row.get("supplier_batch_no") or None,
            "purchase_receipt": receipt.name,
            "lot_status": "Quarantine",
            "description": _("Purchased from {0} on {1}").format(
                receipt.supplier, receipt.posting_date
            ),
        }
    )
    batch.insert(ignore_permissions=True)
    return batch


def _next_lot_id(plant_source, posting_date):
    """`{CODE}-P-{YYYY}-{#####}` -- the purchased counterpart to `-H-` harvests.

    Counting rather than a naming series keeps the two lanes visibly distinct:
    `MEL-H-2026-00001` was grown, `MEL-P-2026-00001` was bought.
    """
    code = plant_source.internal_code or "PS"
    year = getdate(posting_date or nowdate()).year
    prefix = f"{code}-P-{year}-"
    existing = frappe.get_all(
        "Batch", filters={"name": ["like", f"{prefix}%"]}, pluck="name"
    )
    number = len(existing) + 1
    while frappe.db.exists("Batch", f"{prefix}{number:05d}"):
        number += 1
    return f"{prefix}{number:05d}"


def _remember_item_on_source(plant_source, item_code):
    """Keep the source's Raw Material Item in step with what was actually received.

    The harvest automation needs this field to create a lot; filling it from the
    first delivery means a source created during intake is ready for both lanes.
    """
    if not plant_source.raw_material_item:
        frappe.db.set_value("Plant Source", plant_source.name, "raw_material_item", item_code)


def _remember_acquisition_date(plant_source, posting_date):
    if plant_source.acquisition_date:
        return
    frappe.db.set_value(
        "Plant Source", plant_source.name, "acquisition_date", getdate(posting_date)
    )


# ---------------------------------------------------------------------------
# Console API: orders
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def create_purchase_order(supplier, items):
    """Raise and submit a Purchase Order for plant material.

    Submitted immediately because a receipt can only be booked against a
    submitted order, and the Console's flow is "order it, then receive it".
    """
    _require_procurement()
    _validate_supplier(supplier)

    rows = _as_list(items)
    if not rows:
        frappe.throw(_("A purchase order needs at least one line."))

    company = _company()
    warehouse = _raw_materials_warehouse(company)
    order_items = []

    for row in rows:
        item = _stock_item(row.get("item_code"))
        qty, rate = flt(row.get("qty")), flt(row.get("rate"))
        if qty <= 0:
            frappe.throw(_("Quantity must be above zero for {0}.").format(item.name))
        if rate < 0:
            frappe.throw(_("Rate cannot be negative for {0}.").format(item.name))
        order_items.append(
            {
                "item_code": item.name,
                "item_name": item.item_name,
                "qty": qty,
                "rate": rate,
                "uom": item.stock_uom,
                "conversion_factor": 1,
                "warehouse": warehouse,
                "schedule_date": add_days(getdate(nowdate()), 7),
                "plant_source": row.get("plant_source") or None,
                "plant_part": row.get("plant_part") or None,
            }
        )

    order = frappe.get_doc(
        {
            "doctype": "Purchase Order",
            "supplier": supplier,
            "company": company,
            "transaction_date": nowdate(),
            "currency": frappe.get_cached_value(
                "Company", company, "default_currency"
            ),
            "items": order_items,
        }
    )
    order.flags.ignore_permissions = True
    order.insert()
    with system_permissions():
        order.submit()

    return {"name": order.name, "status": order.status}


@frappe.whitelist()
def list_purchase_orders(limit=200):
    _require_procurement()
    limit = _clamp_limit(limit)

    return frappe.get_all(
        "Purchase Order",
        fields=[
            "name",
            "supplier",
            "supplier_name",
            "transaction_date",
            "status",
            "currency",
            "grand_total",
            "per_received",
            "docstatus",
        ],
        filters={"docstatus": ["<", 2]},
        order_by="transaction_date desc, creation desc",
        limit=limit,
    )


@frappe.whitelist()
def get_purchase_order(name):
    """One order with its lines, how much of each has arrived, and its receipts.

    Read through `frappe.get_all` rather than `frappe.get_doc`: Stock Manager's
    native read on Purchase Order is not something this module can rely on
    across ERPNext versions, and the permission this endpoint enforces is the
    Console role, not the doctype's.
    """
    _require_procurement()

    header = frappe.get_all(
        "Purchase Order",
        filters={"name": name},
        fields=[
            "name",
            "supplier",
            "supplier_name",
            "transaction_date",
            "status",
            "currency",
            "grand_total",
            "per_received",
            "docstatus",
            "company",
            "contact_email",
        ],
        limit=1,
    )
    if not header:
        frappe.throw(_("Purchase Order {0} does not exist.").format(name))
    order = header[0]
    order["company_name"] = (
        frappe.get_cached_value("Company", order["company"], "company_name")
        or order["company"]
    )
    # What the Console should pre-fill in "Send to supplier": the contact on the
    # order first, then the supplier's own primary contact.
    order["recipient"] = order.get("contact_email") or _supplier_email(order["supplier"]) or ""
    order["reply_to"] = _reply_to(order) or ""

    received = _received_by_order_item(name)
    order["items"] = frappe.get_all(
        "Purchase Order Item",
        filters={"parent": name},
        fields=[
            "name",
            "item_code",
            "item_name",
            "qty",
            "rate",
            "uom",
            "plant_source",
            "plant_part",
        ],
        order_by="idx asc",
    )
    for row in order["items"]:
        already = flt(received.get(row["name"]))
        row["received_qty"] = already
        row["pending_qty"] = max(0, flt(row["qty"]) - already)

    order["receipts"] = frappe.db.sql(
        """
        SELECT pr.name, pr.posting_date, pr.status, SUM(pri.qty) AS total_qty
        FROM `tabPurchase Receipt` pr
        JOIN `tabPurchase Receipt Item` pri ON pri.parent = pr.name
        WHERE pr.docstatus = 1 AND pri.purchase_order = %s
        GROUP BY pr.name, pr.posting_date, pr.status
        ORDER BY pr.posting_date DESC, pr.creation DESC
        """,
        (name,),
        as_dict=True,
    )
    lots = frappe.get_all(
        "Batch",
        filters={
            "purchase_receipt": ["in", [r["name"] for r in order["receipts"]] or [""]]
        },
        fields=["name", "purchase_receipt", "lot_status", "item"],
    )
    for receipt in order["receipts"]:
        receipt["lots"] = [
            {"name": lot["name"], "item": lot["item"], "lot_status": lot["lot_status"]}
            for lot in lots
            if lot["purchase_receipt"] == receipt["name"]
        ]

    return order


@frappe.whitelist(methods=["POST"])
def send_purchase_order_to_supplier(name, recipient=None, message=None):
    """Email a submitted Purchase Order to its supplier, with the PDF attached.

    Sending is deliberately an explicit action rather than a side effect of
    submit: a PO is recorded the moment it is raised, but whether and when it
    reaches the supplier is a human decision, and the buyer may need to correct
    it first. The address defaults to the contact on the order, then to the
    supplier's primary Contact, and can always be overridden from the Console.

    The PDF is generated as Administrator because the role that buys material
    has no print permission on the ERPNext document, then the mail itself is
    sent as the operator so the Communication record shows who sent it.
    """
    _require_procurement()

    order = frappe.get_doc("Purchase Order", name)
    if order.docstatus != 1:
        frappe.throw(
            _("Submit Purchase Order {0} before sending it.").format(order.name)
        )

    addresses = [
        address.strip()
        for address in (recipient or "").replace(";", ",").split(",")
        if address.strip()
    ]
    if not addresses:
        fallback = order.get("contact_email") or _supplier_email(order.supplier)
        addresses = [fallback] if fallback else []
    if not addresses:
        frappe.throw(
            _(
                "No email address is recorded for supplier {0}. Add a contact with "
                "an email to the supplier, or type an address to send to."
            ).format(order.supplier)
        )
    for address in addresses:
        validate_email_address(address, throw=True)

    company = (
        frappe.get_cached_value("Company", order.company, "company_name")
        or order.company
    )
    subject = _("Purchase Order {0} from {1}").format(order.name, company)
    body = _order_email_body(order, company, message)
    reply_to = _reply_to(order.company)

    try:
        from frappe.utils.pdf import get_pdf

        with system_permissions():
            html = frappe.get_print("Purchase Order", order.name, doc=order)
        attachment = {"fname": f"{order.name}.pdf", "fcontent": get_pdf(html)}
        frappe.sendmail(
            recipients=addresses,
            subject=subject,
            message=body,
            reply_to=reply_to,
            attachments=[attachment],
            reference_doctype="Purchase Order",
            reference_name=order.name,
            now=True,
        )
    except Exception as exc:
        frappe.throw(_("Could not send the order by email: {0}").format(exc))

    # Keep the address on the order so a later notification or re-send does not
    # have to guess it again.
    if addresses[0] != (order.get("contact_email") or ""):
        frappe.db.set_value("Purchase Order", order.name, "contact_email", addresses[0])

    return {"recipients": addresses, "reply_to": reply_to, "purchase_order": order.name}


def _reply_to(company):
    """Where a supplier's reply should land, or None.

    The From address on outgoing mail belongs to the site's sending domain (on
    Frappe Cloud that is the Email Delivery Service's own domain), which is not
    a mailbox anybody reads. Without an explicit Reply-To, a supplier answering
    the order would write into that void.

    The Purchasing section on Company is checked first because ERPNext hides its
    own Email field inside a collapsed "Address & Contact" section, where nobody
    finds it. The company address wins over the operator's so replies reach a
    shared inbox; the operator is the last resort.
    """
    if company:
        # `get` rather than `get_cached_value`: this field is created by
        # setup_purchasing_fields, so it does not exist until the first migrate.
        company_doc = frappe.get_cached_doc("Company", company)
        for fieldname in ("purchase_order_reply_to", "email"):
            address = (company_doc.get(fieldname) or "").strip()
            if address:
                return address

    operator = (frappe.session.user or "").strip()
    if operator and operator != "Administrator" and "@" in operator:
        return operator
    return None


def _order_email_body(order, company, message=None):
    """The email body, with any operator-typed text escaped rather than trusted.

    A custom message replaces the covering note but not the attachment: the PO
    itself is what the supplier needs, the body is only the covering letter.
    """
    if message and message.strip():
        return "<p>{0}</p>".format(
            escape_html(message.strip()).replace("\n", "<br>")
        )

    supplier = escape_html(order.get("supplier_name") or order.supplier)
    total = "{0} {1:,.2f}".format(
        (order.get("currency") or "").upper(), flt(order.grand_total)
    ).strip()
    return _(
        "<p>Dear {0},</p>"
        "<p>Please find attached Purchase Order <strong>{1}</strong> dated {2}.</p>"
        "<p>Order total: {3}.</p>"
        "<p>Kindly confirm receipt and the expected delivery date.</p>"
        "<p>Kind regards,<br>{4}</p>"
    ).format(
        supplier,
        order.name,
        order.transaction_date,
        total,
        escape_html(company),
    )


# ---------------------------------------------------------------------------
# Console API: deliveries
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def record_purchase_intake(items, supplier=None, purchase_order=None, posting_date=None):
    """Book a delivery and turn each line into its own Quarantine lot.

    `purchase_order` is optional on purpose: material is often delivered before
    anyone raises the paperwork, and the traceability record matters more than
    the order being tidy. When an order is given, each line is checked against
    what is still outstanding so a typo cannot silently over-receive.

    A line either names an existing Plant Source or supplies a botanical name,
    in which case one is created for that supplier. Either way it is the
    *source* that is reused -- one per botanical and supplier -- while every
    delivery gets a new lot.
    """
    _require_procurement()

    rows = [row for row in _as_list(items) if flt(row.get("qty")) > 0]
    if not rows:
        frappe.throw(_("Nothing to receive: every line has a quantity of zero."))

    company = _company()
    order = None
    if purchase_order:
        order = frappe.get_doc("Purchase Order", purchase_order)
        if order.docstatus != 1:
            frappe.throw(
                _("Purchase Order {0} is not submitted, so it cannot be received.").format(
                    purchase_order
                )
            )
        supplier = supplier or order.supplier
        if supplier != order.supplier:
            frappe.throw(
                _("Supplier {0} does not match Purchase Order {1} ({2}).").format(
                    supplier, order.name, order.supplier
                )
            )

    if not supplier:
        frappe.throw(_("Select the supplier this delivery came from."))
    _validate_supplier(supplier)

    posting = getdate(posting_date or nowdate())
    warehouse = _raw_materials_warehouse(company)
    outstanding = _outstanding_by_order_item(order) if order else {}

    receipt_items = []
    for row in rows:
        item = _stock_item(row.get("item_code"))
        qty = flt(row.get("qty"))
        rate = flt(row.get("rate"))
        po_line = row.get("purchase_order_item")

        if order and not po_line:
            po_line = _match_open_order_line(order, item.name, outstanding)
        if order and po_line:
            if po_line not in outstanding:
                frappe.throw(
                    _("Line {0} does not belong to Purchase Order {1}.").format(
                        po_line, order.name
                    )
                )
            left = outstanding[po_line]
            if qty > left + 0.001:
                frappe.throw(
                    _("{0}: {1} {2} is more than the {3} {2} still outstanding on this order.").format(
                        item.name,
                        _format_qty(qty),
                        item.stock_uom,
                        _format_qty(left),
                    )
                )
            outstanding[po_line] = left - qty

        plant_source = _resolve_plant_source(
            item=item,
            supplier=supplier,
            plant_source=row.get("plant_source"),
            botanical_name=row.get("botanical_name"),
            posting_date=posting,
        )

        receipt_items.append(
            {
                "item_code": item.name,
                "item_name": item.item_name,
                "qty": qty,
                "rate": rate,
                "uom": item.stock_uom,
                "conversion_factor": 1,
                "warehouse": warehouse,
                "plant_source": plant_source.name,
                "plant_part": row.get("plant_part") or None,
                "supplier_batch_no": row.get("supplier_batch_no") or None,
                "purchase_order": order.name if order else None,
                "purchase_order_item": po_line or None,
                "allow_zero_valuation_rate": 1 if rate == 0 else 0,
            }
        )

    receipt = frappe.get_doc(
        {
            "doctype": "Purchase Receipt",
            "supplier": supplier,
            "company": company,
            "posting_date": posting,
            "posting_time": nowtime(),
            "set_posting_time": 1,
            "items": receipt_items,
        }
    )
    receipt.flags.ignore_permissions = True
    receipt.insert()

    # Lots are created while the receipt is still a draft so they carry the
    # operator as owner; `before_submit` then sees a batch_no and leaves them
    # alone. Submitting under system permissions is the same workaround the
    # extraction automation documents: ERPNext materialises a Serial and Batch
    # Bundle on submit and permission-checks the *session* user for it.
    lots = []
    for row in receipt.items:
        plant_source = _plant_source_doc(row.plant_source)
        batch = _create_lot(plant_source, row, receipt)
        row.batch_no = batch.name
        row.use_serial_batch_fields = 1
        _remember_item_on_source(plant_source, row.item_code)
        _remember_acquisition_date(plant_source, posting)
        lots.append(
            {"batch": batch.name, "item_code": row.item_code, "plant_source": plant_source.name}
        )

    receipt.save()
    with system_permissions():
        receipt.submit()

    return {"purchase_receipt": receipt.name, "lots": lots}


def _resolve_plant_source(item, supplier, plant_source, botanical_name, posting_date):
    """Find (or create) the one Plant Source this delivery belongs to.

    Existing source wins in this order: the one the operator picked, then the
    one already recorded for this item + supplier, then one recorded for the
    same botanical + supplier (a source entered before its item was linked).
    Only if none exists is a new source created, keyed on the botanical name so
    the same herb from the same supplier never ends up as two sources.
    """
    if plant_source:
        source = _plant_source_doc(plant_source)
        if source.grower_supplier and source.grower_supplier != supplier:
            frappe.throw(
                _("Plant Source {0} belongs to {1}, not {2}.").format(
                    source.name, source.grower_supplier, supplier
                )
            )
        return source

    botanical = (botanical_name or item.get("botanical_name") or "").strip()

    existing = frappe.db.get_value(
        "Plant Source",
        {
            "raw_material_item": item.name,
            "grower_supplier": supplier,
            "source_type": "Purchased",
        },
        "name",
        order_by="creation asc",
    )
    if not existing and botanical:
        existing = frappe.db.get_value(
            "Plant Source",
            {
                "botanical_name": botanical,
                "grower_supplier": supplier,
                "source_type": "Purchased",
            },
            "name",
            order_by="creation asc",
        )
    if existing:
        return _plant_source_doc(existing)

    if not botanical:
        frappe.throw(
            _(
                "No Plant Source is recorded for {0} from {1}. Pick one, or give "
                "the botanical name so a source can be created for this delivery."
            ).format(item.name, supplier)
        )

    source = frappe.get_doc(
        {
            "doctype": "Plant Source",
            "internal_code": _unique_internal_code(botanical),
            "botanical_name": botanical,
            "source_type": "Purchased",
            "grower_supplier": supplier,
            "raw_material_item": item.name,
            "acquisition_date": getdate(posting_date or nowdate()),
        }
    )
    source.insert(ignore_permissions=True)
    return source


def _unique_internal_code(botanical_name):
    """Plant Source is named by `internal_code`, which is unique.

    The first word of the botanical name is a sensible default (`Melissa`), and
    a numeric suffix keeps a second source distinct without asking the operator
    to invent a code at the goods-in desk.
    """
    base = re.sub(r"[^A-Za-z0-9]", "", (botanical_name or "").split(" ")[0]).upper()[:10]
    base = base or "PS"
    code, suffix = base, 2
    while frappe.db.exists("Plant Source", code):
        code = f"{base[:8]}{suffix}"
        suffix += 1
    return code


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _require_procurement():
    if not PROCUREMENT_ROLES.intersection(frappe.get_roles()):
        frappe.throw(
            _("Not permitted: this account cannot record supplier orders or deliveries."),
            frappe.PermissionError,
        )


def _company():
    company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_value("Company", {}, "name", order_by="creation asc")
    if not company:
        frappe.throw(_("No company is configured on this site."))
    return company


def _raw_materials_warehouse(company):
    abbr = frappe.get_cached_value("Company", company, "abbr")
    for filters in (
        {"company": company, "name": f"Raw Materials - {abbr}", "is_group": 0},
        {"company": company, "name": ["like", "%Raw Materials%"], "is_group": 0, "disabled": 0},
    ):
        warehouse = frappe.db.get_value("Warehouse", filters, "name", order_by="name asc")
        if warehouse:
            return warehouse
    frappe.throw(_(RAW_MATERIALS_WAREHOUSE_HINT).format(abbr=abbr))


def _stock_item(item_code):
    """An item that can carry a lot. Both conditions are what make the
    traceability chain possible, so they fail here with a usable message rather
    than deep inside a stock entry."""
    if not item_code:
        frappe.throw(_("Every line needs an item."))
    if not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item {0} does not exist.").format(item_code))

    item = frappe.get_cached_doc("Item", item_code)
    if not item.is_stock_item:
        frappe.throw(
            _("Item {0} is not a stock item, so it cannot be received.").format(item.name)
        )
    if not item.has_batch_no:
        frappe.throw(
            _(
                "Item {0} is not batch-tracked. Enable 'Has Batch No' on the item "
                "first -- otherwise the delivery cannot become a traceable lot."
            ).format(item.name)
        )
    return item


def _plant_source_doc(name):
    if name and frappe.db.exists("Plant Source", name):
        return frappe.get_cached_doc("Plant Source", name)
    frappe.throw(_("Plant Source {0} does not exist.").format(name))


def _validate_supplier(supplier):
    if not supplier or not frappe.db.exists("Supplier", supplier):
        frappe.throw(_("Supplier {0} does not exist.").format(supplier))
    if frappe.db.get_value("Supplier", supplier, "disabled"):
        frappe.throw(_("Supplier {0} is disabled.").format(supplier))


def _supplier_email(supplier):
    """The supplier's primary Contact email, or None.

    Contacts attach to a Supplier through the Dynamic Link child table, so this
    is not a column on Supplier itself. Primary contacts win; among the rest,
    the oldest is as good a default as any and is stable across requests.
    """
    rows = frappe.db.sql(
        """
        SELECT c.email_id
        FROM `tabContact` c
        JOIN `tabDynamic Link` dl
          ON dl.parent = c.name AND dl.parenttype = 'Contact'
        WHERE dl.link_doctype = 'Supplier'
          AND dl.link_name = %s
          AND c.email_id IS NOT NULL
          AND c.email_id != ''
        ORDER BY c.is_primary_contact DESC, c.creation ASC
        LIMIT 1
        """,
        (supplier,),
        as_dict=True,
    )
    return rows[0].email_id if rows else None


def _received_by_order_item(purchase_order):
    rows = frappe.db.sql(
        """
        SELECT pri.purchase_order_item, SUM(pri.qty) AS qty
        FROM `tabPurchase Receipt Item` pri
        JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
        WHERE pr.docstatus = 1 AND pri.purchase_order = %s
        GROUP BY pri.purchase_order_item
        """,
        (purchase_order,),
        as_dict=True,
    )
    return {row.purchase_order_item: flt(row.qty) for row in rows}


def _outstanding_by_order_item(order):
    received = _received_by_order_item(order.name)
    return {
        row.name: max(0, flt(row.qty) - flt(received.get(row.name)))
        for row in order.items
    }


def _match_open_order_line(order, item_code, outstanding):
    """Pick the first line of the order for this item that still has room.

    The Console sends `purchase_order_item` explicitly; this is the fallback for
    a caller that only knows the item (a Desk script, or an amended order whose
    row names changed).
    """
    for row in order.items:
        if row.item_code != item_code:
            continue
        if outstanding.get(row.name, flt(row.qty)) > 0:
            return row.name
    return None


def _format_qty(value):
    return f"{flt(value):g}"


def _clamp_limit(limit, ceiling=500, default=200):
    try:
        return max(1, min(int(limit), ceiling))
    except (TypeError, ValueError):
        return default


def _as_list(value):
    loaded = value
    if isinstance(value, str):
        loaded = json.loads(value)
    return list(loaded or [])
