"""Whitelisted endpoints for the Symbiotope Console frontend.

Two kinds of thing live here, and the difference matters.

`get_my_context` is UX-only: it tells the frontend which nav items and actions
to show. It is not what enforces anything -- the reads and writes it leads to
still go through Frappe's own Role/DocPerm checks on the resource they target,
exactly as if the request came from Desk.

The stock and supplier readers below are different, and are a deliberate
exception to that rule. The Console's operational roles (Grower Harvester,
Production Operator, QA) hold no permission on Bin, Warehouse, Supplier or
Stock Ledger Entry, so an operator cannot see stock levels at all. The
alternatives were granting those roles ERPNext's own Stock User -- which also
lets them create and submit Stock Entries by hand in Desk, undercutting the
rule that stock only ever moves through the automation -- or adding Custom
DocPerm rows, which make Frappe ignore every native row on those doctypes from
then on.

So these read *as the system* (`frappe.get_all` does not check permissions) and
gate on holding a Console role instead. They are deliberately narrow: read-only,
no arbitrary doctype or filter from the caller, and they return only the fields
the Console renders. Anything that writes stays on the DocPerm path.
"""

import frappe

#: Holding any one of these is what the readers below check for. Membership is
#: still decided in ERPNext -- this only says which roles the Console serves.
CONSOLE_ROLES = frozenset({
    "Grower Harvester",
    "Production Operator",
    "QA",
    "Symbiotope Admin",
    "System Manager",
})


def _require_console_role():
    if not CONSOLE_ROLES.intersection(frappe.get_roles()):
        frappe.throw(
            "Not permitted: this account holds no Symbiotope Console role.",
            frappe.PermissionError,
        )


@frappe.whitelist()
def get_my_context():
    user = frappe.session.user
    return {
        "email": user,
        "full_name": frappe.db.get_value("User", user, "full_name") or user,
        "roles": frappe.get_roles(user),
    }


@frappe.whitelist()
def get_stock_overview():
    """Per-warehouse stock on hand, plus the warehouses themselves.

    Warehouses are returned separately so one holding nothing still shows as a
    row with zero rather than vanishing -- an empty Extract Storage is a fact
    worth seeing, not an absence.
    """
    _require_console_role()

    rows = frappe.get_all(
        "Bin",
        fields=["item_code", "warehouse", "actual_qty", "stock_uom"],
        filters={"actual_qty": ["!=", 0]},
        order_by="warehouse asc, item_code asc",
    )

    item_names = {
        item.name: item.item_name
        for item in frappe.get_all("Item", fields=["name", "item_name"])
    }
    for row in rows:
        row["item_name"] = item_names.get(row["item_code"])

    warehouses = frappe.get_all(
        "Warehouse",
        fields=["name", "warehouse_name"],
        filters={"is_group": 0, "disabled": 0},
        order_by="name asc",
    )

    return {"warehouses": warehouses, "rows": rows}


def _safe_get_all(doctype, **kwargs):
    """A lookup whose failure must not take the whole screen down.

    The lot resolution below reaches into ERPNext's own internals, and those
    move between versions. If one of those reads fails, the movements list
    should still render with the lots it could resolve rather than 500 -- but
    the failure is logged rather than swallowed, because a column of dashes
    with nothing in the log is how a defect goes unnoticed.
    """
    try:
        return frappe.get_all(doctype, **kwargs)
    except Exception:
        frappe.log_error(
            title="Symbiotope Console: lot lookup failed",
            message=f"{doctype}: {frappe.get_traceback()}",
        )
        return []


@frappe.whitelist()
def get_stock_movements(limit=100):
    """Recent stock movements, each carrying the lot that moved.

    The lot is the whole point: it is what ties a quantity in a warehouse back
    to the harvest or extraction that produced it, which is the question this
    system exists to answer.

    Getting it is not as simple as reading Stock Ledger Entry.batch_no. In
    ERPNext v16 that column stays empty -- measured, not assumed: all 19
    movements on this site had it null -- because the lot is recorded on the
    Serial and Batch Bundle the submit materialises instead. That is the same
    v16 behaviour that caused the permission failure in the extraction hook.

    So the lot is resolved in three passes, cheapest first: the column itself
    where some other code path did populate it, then the bundle, then the Stock
    Entry row the ledger entry came from. A movement with no lot at all is an
    untracked item, which is a real answer rather than a lookup failure.
    """
    _require_console_role()

    # Clamp rather than trust: limit arrives as a string over HTTP, and an
    # unbounded value here would pull the entire ledger into the browser.
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100

    rows = frappe.get_all(
        "Stock Ledger Entry",
        fields=[
            "item_code",
            "warehouse",
            "batch_no",
            "actual_qty",
            "posting_date",
            "voucher_type",
            "voucher_no",
            "serial_and_batch_bundle",
            "voucher_detail_no",
        ],
        filters={"is_cancelled": 0},
        order_by="posting_date desc, creation desc",
        limit=limit,
    )

    unresolved = [row for row in rows if not row.get("batch_no")]

    # Pass 2: the Serial and Batch Bundle. One bundle can hold several lots;
    # the Console shows one per movement, so a split bundle is reported as its
    # first lot rather than silently as none.
    bundle_ids = [
        row["serial_and_batch_bundle"]
        for row in unresolved
        if row.get("serial_and_batch_bundle")
    ]
    bundle_lots = {}
    if bundle_ids:
        # No parent kwarg: frappe.get_all does not take one, and it reads child
        # tables directly because it does not run permission checks at all.
        for entry in _safe_get_all(
            "Serial and Batch Entry",
            filters={"parent": ["in", bundle_ids]},
            fields=["parent", "batch_no"],
        ):
            if entry.get("batch_no"):
                bundle_lots.setdefault(entry["parent"], entry["batch_no"])

    # Pass 3: the Stock Entry row itself, which carries batch_no directly when
    # the automation set use_serial_batch_fields.
    detail_ids = [
        row["voucher_detail_no"]
        for row in unresolved
        if row.get("voucher_detail_no")
        and not bundle_lots.get(row.get("serial_and_batch_bundle"))
    ]
    detail_lots = {}
    if detail_ids:
        for detail in _safe_get_all(
            "Stock Entry Detail",
            filters={"name": ["in", detail_ids]},
            fields=["name", "batch_no"],
        ):
            if detail.get("batch_no"):
                detail_lots[detail["name"]] = detail["batch_no"]

    for row in rows:
        if not row.get("batch_no"):
            row["batch_no"] = bundle_lots.get(
                row.get("serial_and_batch_bundle")
            ) or detail_lots.get(row.get("voucher_detail_no"))
        # Plumbing, not something the Console renders.
        row.pop("serial_and_batch_bundle", None)
        row.pop("voucher_detail_no", None)

    return rows


@frappe.whitelist()
def get_suppliers():
    """The supplier list, for the Suppliers screen.

    Plant Source is readable by the Console roles already, so the link from a
    supplier to what it supplies is queried directly by the frontend rather
    than assembled here.
    """
    _require_console_role()

    return frappe.get_all(
        "Supplier",
        fields=["name", "supplier_name", "supplier_group", "country", "disabled"],
        order_by="supplier_name asc",
    )
