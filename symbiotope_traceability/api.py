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


@frappe.whitelist()
def get_stock_movements(limit=100):
    """Recent stock movements, each carrying the batch that moved.

    batch_no is the whole point: it is what ties a quantity in a warehouse back
    to the harvest or extraction that produced it, which is the question this
    system exists to answer.
    """
    _require_console_role()

    # Clamp rather than trust: limit arrives as a string over HTTP, and an
    # unbounded value here would pull the entire ledger into the browser.
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100

    return frappe.get_all(
        "Stock Ledger Entry",
        fields=[
            "item_code",
            "warehouse",
            "batch_no",
            "actual_qty",
            "posting_date",
            "voucher_type",
            "voucher_no",
        ],
        filters={"is_cancelled": 0},
        order_by="posting_date desc, creation desc",
        limit=limit,
    )


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
