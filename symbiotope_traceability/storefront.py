"""Storefront <-> ERPNext integration (Milestone 5).

Two callers talk to this module, and they get different doors:

- The storefronts (Netlify, server-side) call `place_order`, `cancel_order`,
  `get_available_stock` and `get_order_status`. They hold no Frappe session, so
  the write endpoints are guarded by a shared secret in site config, and the
  read endpoints are guest-readable but return only what the caller needs.
- The Symbiotope Console calls `list_orders`, `get_order`, `ship_order`,
  `receive_finished_goods`, `resend_shipping_notice` and `tag_warehouse_for_brand`,
  as a signed-in admin. Those check the Console admin roles before doing anything.

Stock model. An order reserves stock in its brand's finished-goods warehouse
when it is placed (Sales Order `reserve_stock`), so two customers cannot both
buy the last unit. Stock physically leaves the warehouse when staff ship it
(Delivery Note). Cancelling an order before shipment releases the reservation.
If an order arrives when stock is already short (a race, or payment that
outlived the stock check), it is still taken, flagged `stock_shortfall`, and
shown to staff rather than rejected after the customer has paid.

Config (site_config.json, set in Frappe Cloud):
- storefront_erp_secret: the storefronts send this as X-Symbiotope-Secret.
- storefront_notify_secret: this app sends it to the storefronts when an order ships.
- storefront_base_url: where the storefronts live, e.g. https://symbiotope.example.
"""

import hmac
import json

import frappe
import requests
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import add_days, flt, nowdate

from symbiotope_traceability.automation import system_permissions

ADMIN_ROLES = frozenset({"Symbiotope Admin", "System Manager"})
# Who manages orders: shipping them, and reading them. Stock receiving stays admin-only.
ORDER_ROLES = ADMIN_ROLES | {"Sales Manager", "Stock Manager", "Delivery Manager"}
SECRET_HEADER = "X-Symbiotope-Secret"
MAX_SKUS_PER_STOCK_CHECK = 100
SHIPMENT_STATUSES = ("Unfulfilled", "Partially Shipped", "Shipped", "Cancelled")


# ---------------------------------------------------------------------------
# Setup (runs after every migrate, so custom fields are always present)
# ---------------------------------------------------------------------------


def setup_custom_fields():
    create_custom_fields(
        {
            "Sales Order": [
                {"fieldname": "storefront_section", "fieldtype": "Section Break",
                 "label": "Storefront", "insert_after": "customer_name", "collapsible": 1},
                {"fieldname": "brand_tag", "fieldtype": "Data", "label": "Brand Tag",
                 "insert_after": "storefront_section", "read_only": 1, "search_index": 1},
                {"fieldname": "stripe_session_id", "fieldtype": "Data", "label": "Stripe Checkout Session",
                 "insert_after": "brand_tag", "read_only": 1, "search_index": 1},
                {"fieldname": "stripe_payment_intent", "fieldtype": "Data", "label": "Stripe Payment Intent",
                 "insert_after": "stripe_session_id", "read_only": 1, "search_index": 1},
                {"fieldname": "customer_email", "fieldtype": "Data", "label": "Customer Email",
                 "insert_after": "stripe_payment_intent", "read_only": 1},
                {"fieldname": "language", "fieldtype": "Data", "label": "Language",
                 "insert_after": "customer_email", "read_only": 1},
                {"fieldname": "status_token", "fieldtype": "Data", "label": "Order Status Token",
                 "insert_after": "language", "read_only": 1, "search_index": 1},
                {"fieldname": "stock_shortfall", "fieldtype": "Check", "label": "Stock Shortfall (review)",
                 "insert_after": "status_token", "read_only": 1},
                {"fieldname": "shipping_status", "fieldtype": "Select", "label": "Shipping Status",
                 "options": "\n".join(SHIPMENT_STATUSES), "default": "Unfulfilled",
                 "insert_after": "stock_shortfall", "read_only": 1},
                {"fieldname": "shipping_notice_sent", "fieldtype": "Check", "label": "Shipping Notice Sent",
                 "insert_after": "shipping_status", "read_only": 1},
            ],
            "Warehouse": [
                {"fieldname": "brand_tag", "fieldtype": "Data", "label": "Brand Tag",
                 "insert_after": "warehouse_name", "search_index": 1,
                 "description": "The storefront brand this warehouse ships for."},
            ],
            "Delivery Note": [
                {"fieldname": "carrier", "fieldtype": "Data", "label": "Carrier",
                 "insert_after": "customer_name"},
                {"fieldname": "tracking_number", "fieldtype": "Data", "label": "Tracking Number",
                 "insert_after": "carrier"},
            ],
        },
        update=True,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _require_secret(config_key):
    expected = frappe.conf.get(config_key)
    if not expected:
        frappe.throw(_("Storefront integration is not configured on this site."), frappe.ValidationError)
    supplied = frappe.get_request_header(SECRET_HEADER) or ""
    if not hmac.compare_digest(supplied, expected):
        frappe.throw(_("Not permitted."), frappe.PermissionError)


def _require_admin():
    if not ADMIN_ROLES.intersection(frappe.get_roles()):
        frappe.throw(_("Not permitted: this account holds no Symbiotope Console admin role."),
                     frappe.PermissionError)


def _require_order_access():
    if not ORDER_ROLES.intersection(frappe.get_roles()):
        frappe.throw(_("Not permitted: this account cannot manage orders."), frappe.PermissionError)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return list(value)


def _brand_warehouses(brand_tag):
    if not brand_tag:
        return []
    return frappe.get_all(
        "Warehouse",
        filters={"brand_tag": brand_tag, "is_group": 0, "disabled": 0},
        pluck="name",
        order_by="name asc",
    )


def _available_by_warehouse(warehouses, skus):
    """Actual minus reserved, per (sku, warehouse). Only ever reads Bin."""
    result = {(sku, wh): 0.0 for sku in skus for wh in warehouses}
    if not warehouses or not skus:
        return result
    for row in frappe.get_all(
        "Bin",
        filters={"item_code": ["in", skus], "warehouse": ["in", warehouses]},
        fields=["item_code", "warehouse", "actual_qty", "reserved_qty"],
    ):
        result[(row.item_code, row.warehouse)] = max(
            0.0, flt(row.actual_qty) - flt(row.reserved_qty)
        )
    return result


def _pick_warehouse(available, sku, qty, warehouses):
    """The brand warehouse holding the most of this SKU, if it covers the quantity."""
    best = max(warehouses, key=lambda wh: available[(sku, wh)], default=None)
    if best is not None and available[(sku, best)] >= qty:
        return best
    return None


def _get_or_create_customer(email, full_name):
    existing = frappe.db.get_value("Customer", {"customer_name": email}, "name")
    if existing:
        return existing
    customer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": email,
        "customer_type": "Individual",
        "customer_group": frappe.db.get_single_value("Selling Settings", "customer_group") or "Individual",
        "territory": frappe.db.get_single_value("Selling Settings", "territory") or "All Territories",
    })
    customer.insert(ignore_permissions=True)
    return customer.name


def _notify_storefront_shipped(sales_order_name, delivery_note_name):
    """Ask the storefront to send the shipping email. Returns True on success.

    The storefront reads the order back from here by its status token, so only
    the token and the delivery note go over the wire.
    """
    base_url = frappe.conf.get("storefront_base_url")
    secret = frappe.conf.get("storefront_notify_secret")
    token = frappe.db.get_value("Sales Order", sales_order_name, "status_token")
    if not (base_url and secret and token):
        frappe.log_error(
            title="Storefront shipping notice not sent: not configured",
            message=f"{sales_order_name} / {delivery_note_name}",
        )
        return False
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/orders/shipped",
            json={"status_token": token, "delivery_note": delivery_note_name},
            headers={SECRET_HEADER: secret},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException:
        frappe.log_error(
            title="Storefront shipping notice failed",
            message=f"{sales_order_name} / {delivery_note_name}\n{frappe.get_traceback()}",
        )
        return False
    frappe.db.set_value("Sales Order", sales_order_name, "shipping_notice_sent", 1)
    return True


def _has_delivery(sales_order_name):
    return bool(frappe.db.exists(
        "Delivery Note Item",
        {"against_sales_order": sales_order_name, "docstatus": 1},
    ))


# ---------------------------------------------------------------------------
# Storefront-facing endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_available_stock(brand_tag, skus):
    """Units a customer can still buy, per SKU, for one brand. Numbers only."""
    skus = [str(s) for s in _as_list(skus)][:MAX_SKUS_PER_STOCK_CHECK]
    warehouses = _brand_warehouses(brand_tag)
    available = _available_by_warehouse(warehouses, skus)
    return {
        sku: sum(available[(sku, wh)] for wh in warehouses)
        for sku in skus
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def place_order(stripe_checkout_session_id, stripe_payment_intent, brand_tag, currency,
                customer_email, language, items, customer_name=None):
    """Create and submit a Sales Order for a paid Stripe Checkout Session.

    Idempotent on the session id, so a retried webhook does not double-order.
    `items` is [{sku, qty, unit_price}], with unit_price in major currency units
    as Stripe charged it.
    """
    _require_secret("storefront_erp_secret")

    existing = frappe.db.get_value(
        "Sales Order",
        {"stripe_session_id": stripe_checkout_session_id},
        ["name", "status_token", "stock_shortfall"],
        as_dict=True,
    )
    if existing:
        return {"name": existing.name, "status_token": existing.status_token,
                "stock_shortfall": bool(existing.stock_shortfall), "duplicate": True}

    items = _as_list(items)
    if not items:
        frappe.throw(_("An order needs at least one item."))

    company = frappe.defaults.get_global_default("company")
    company_currency = frappe.db.get_value("Company", company, "default_currency")
    if (currency or "").upper() != (company_currency or "").upper():
        frappe.throw(_("Storefront currency {0} does not match the ERP company currency {1}.")
                     .format(currency, company_currency))

    skus = [str(i["sku"]) for i in items]
    for sku in skus:
        if not frappe.db.exists("Item", sku):
            frappe.throw(_("No ERP item for SKU {0}. Create the item before selling it.").format(sku))

    warehouses = _brand_warehouses(brand_tag)
    if not warehouses:
        frappe.throw(_("No warehouse is tagged for brand {0}.").format(brand_tag))

    available = _available_by_warehouse(warehouses, skus)
    shortfall = False
    lines = []
    for item in items:
        sku, qty = str(item["sku"]), int(item["qty"])
        warehouse = _pick_warehouse(available, sku, qty, warehouses)
        if warehouse is None:
            shortfall = True
            warehouse = max(warehouses, key=lambda wh: available[(sku, wh)])
        else:
            available[(sku, warehouse)] -= qty  # so two lines for one SKU do not double-count
        lines.append({
            "item_code": sku,
            "qty": qty,
            "rate": flt(item["unit_price"]),
            "warehouse": warehouse,
            "delivery_date": add_days(nowdate(), 7),
        })

    with system_permissions():
        customer = _get_or_create_customer(customer_email, customer_name or customer_email)
        order = frappe.get_doc({
            "doctype": "Sales Order",
            "customer": customer,
            "company": company,
            "order_type": "Sales",
            "transaction_date": nowdate(),
            "delivery_date": add_days(nowdate(), 7),
            "currency": currency.upper(),
            "items": lines,
            "reserve_stock": 0 if shortfall else 1,
            "brand_tag": brand_tag,
            "stripe_session_id": stripe_checkout_session_id,
            "stripe_payment_intent": stripe_payment_intent,
            "customer_email": customer_email,
            "language": language,
            "status_token": frappe.generate_hash(length=32),
            "stock_shortfall": 1 if shortfall else 0,
            "shipping_status": "Unfulfilled",
        })
        order.insert(ignore_permissions=True)
        order.submit()

    return {"name": order.name, "status_token": order.status_token,
            "stock_shortfall": shortfall, "duplicate": False}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def cancel_order(stripe_payment_intent, reason=None):
    """Cancel the order behind a refunded Stripe payment, releasing its stock.

    Refused, and logged for staff, once anything has shipped: returning goods is
    a physical process, so the refund then needs handling by hand.
    """
    _require_secret("storefront_erp_secret")

    name = frappe.db.get_value("Sales Order", {"stripe_payment_intent": stripe_payment_intent}, "name")
    if not name:
        return {"cancelled": False, "reason": "not_found"}

    order = frappe.get_doc("Sales Order", name)
    if order.docstatus == 2:
        return {"cancelled": True, "name": name, "reason": "already_cancelled"}
    if _has_delivery(name):
        frappe.log_error(
            title="Storefront refund needs manual handling",
            message=f"{name} was refunded in Stripe but has shipped. Stock was not restored.\n"
                    f"Reason given: {reason or 'none'}",
        )
        return {"cancelled": False, "name": name, "reason": "already_shipped"}

    with system_permissions():
        order.cancel()
    frappe.db.set_value("Sales Order", name, "shipping_status", "Cancelled")
    return {"cancelled": True, "name": name, "reason": "cancelled"}


@frappe.whitelist(allow_guest=True)
def get_order_status(status_token):
    """What a customer sees on their order link. Only this order, read by its token."""
    if not status_token or len(status_token) < 32:
        return None
    name = frappe.db.get_value("Sales Order", {"status_token": status_token}, "name")
    if not name:
        return None
    # The token is the customer's own key to this order, so the contact details
    # the storefront needs to email them come back here too.
    summary = _order_summary(name, include_customer=False)
    contact = frappe.db.get_value("Sales Order", name, ["customer_email", "language", "brand_tag"], as_dict=True)
    summary.update({
        "customer_email": contact.customer_email,
        "language": contact.language or "en",
        "brand_tag": contact.brand_tag,
    })
    return summary


def _order_summary(name, include_customer):
    order = frappe.get_doc("Sales Order", name)
    summary = {
        "order": order.name,
        "placed_on": str(order.transaction_date),
        "currency": order.currency,
        "total": flt(order.grand_total),
        "cancelled": order.docstatus == 2,
        "shipping_status": order.shipping_status or "Unfulfilled",
        "items": [
            {"sku": i.item_code, "name": i.item_name, "qty": flt(i.qty),
             "shipped_qty": flt(i.delivered_qty), "rate": flt(i.rate)}
            for i in order.items
        ],
        "shipments": _shipments(name),
    }
    if include_customer:
        summary.update({
            "brand_tag": order.brand_tag,
            "customer_email": order.customer_email,
            "stripe_payment_intent": order.stripe_payment_intent,
            "stock_shortfall": bool(order.stock_shortfall),
            "shipping_notice_sent": bool(order.shipping_notice_sent),
        })
    return summary


def _shipments(sales_order_name):
    rows = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_order": sales_order_name, "docstatus": 1},
        fields=["parent", "item_code", "qty", "warehouse"],
        order_by="creation asc",
    )
    if not rows:
        return []
    items_by_note = {}
    for row in rows:
        items_by_note.setdefault(row.parent, []).append(
            {"sku": row.item_code, "qty": flt(row.qty), "warehouse": row.warehouse})
    notes = frappe.get_all(
        "Delivery Note",
        filters={"name": ["in", list(items_by_note)]},
        fields=["name", "posting_date", "carrier", "tracking_number"],
        order_by="creation asc",
    )
    return [
        {"delivery_note": n.name, "date": str(n.posting_date), "carrier": n.carrier,
         "tracking_number": n.tracking_number, "items": items_by_note[n.name]}
        for n in notes
    ]


# ---------------------------------------------------------------------------
# Console-facing endpoints (order roles, or admin only where noted)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_orders(limit=100):
    _require_order_access()
    limit = max(1, min(int(limit), 500))
    return frappe.get_all(
        "Sales Order",
        filters={"stripe_session_id": ["is", "set"]},
        fields=["name", "transaction_date", "customer_name", "customer_email", "brand_tag",
                "grand_total", "currency", "status", "shipping_status", "stock_shortfall",
                "docstatus"],
        order_by="creation desc",
        limit=limit,
    )


@frappe.whitelist()
def get_order(name):
    _require_order_access()
    if not frappe.db.exists("Sales Order", name):
        frappe.throw(_("No order {0}.").format(name), frappe.DoesNotExistError)
    return _order_summary(name, include_customer=True)


@frappe.whitelist(methods=["POST"])
def ship_order(sales_order, shipments, carrier=None, tracking_number=None):
    """Ship some or all of an order, from one or more warehouses.

    `shipments` is [{warehouse, carrier?, tracking_number?, items: [{sku, qty}]}].
    Each entry becomes its own Delivery Note, so a split order ships as separate
    parcels, each with its own carrier and tracking number.
    """
    _require_order_access()
    order = frappe.get_doc("Sales Order", sales_order)
    if order.docstatus != 1:
        frappe.throw(_("Only a submitted order can be shipped."))

    from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

    notes = []
    with system_permissions():
        for shipment in _as_list(shipments):
            warehouse = shipment["warehouse"]
            requested = {str(i["sku"]): flt(i["qty"]) for i in _as_list(shipment.get("items"))}
            requested = {sku: qty for sku, qty in requested.items() if qty > 0}
            if not requested:
                continue

            note = make_delivery_note(order.name)
            remaining_rows = []
            for row in note.items:
                wanted = requested.pop(row.item_code, 0)
                if wanted <= 0:
                    continue
                if wanted > flt(row.qty):
                    frappe.throw(_("Cannot ship {0} of {1}: only {2} left to ship.")
                                 .format(wanted, row.item_code, flt(row.qty)))
                row.qty = wanted
                row.warehouse = warehouse
                remaining_rows.append(row)
            if requested:
                frappe.throw(_("These items are not on order {0}: {1}")
                             .format(order.name, ", ".join(requested)))
            if not remaining_rows:
                continue

            note.items = remaining_rows
            # Each parcel can carry its own carrier and tracking number; the
            # top-level values are the default for a single-parcel order.
            note.carrier = shipment.get("carrier") or carrier
            note.tracking_number = shipment.get("tracking_number") or tracking_number
            note.insert(ignore_permissions=True)
            note.submit()
            notes.append(note.name)

        order.reload()
        if order.docstatus == 1:
            shipped_all = all(flt(i.delivered_qty) >= flt(i.qty) for i in order.items)
            frappe.db.set_value("Sales Order", order.name,
                                "shipping_status", "Shipped" if shipped_all else "Partially Shipped")

    notified = {n: _notify_storefront_shipped(order.name, n) for n in notes} if notes else {}
    return {"delivery_notes": notes, "notification_sent": bool(notes) and all(notified.values())}


@frappe.whitelist(methods=["POST"])
def resend_shipping_notice(sales_order):
    _require_order_access()
    latest = frappe.db.get_value(
        "Delivery Note Item",
        {"against_sales_order": sales_order, "docstatus": 1},
        "parent",
        order_by="creation desc",
    )
    if not latest:
        frappe.throw(_("Nothing has shipped on {0} yet.").format(sales_order))
    return {"notification_sent": _notify_storefront_shipped(sales_order, latest)}


@frappe.whitelist(methods=["POST"])
def receive_finished_goods(item_code, warehouse, qty, basic_rate):
    """Put finished, made product into a brand's warehouse, so it can be sold.

    Batch-tracked items get a batch for the day's receipt, created here.
    """
    _require_admin()
    qty, basic_rate = flt(qty), flt(basic_rate)
    if qty <= 0 or basic_rate <= 0:
        frappe.throw(_("Quantity and cost per unit must both be above zero."))
    if not frappe.db.get_value("Warehouse", warehouse, "brand_tag"):
        frappe.throw(_("{0} is not tagged to a brand, so its stock cannot be sold.").format(warehouse))

    item = frappe.get_doc("Item", item_code)
    row = {"item_code": item_code, "t_warehouse": warehouse, "qty": qty, "basic_rate": basic_rate}
    if item.has_batch_no:
        batch_name = f"{item_code}-FG-{nowdate()}"
        if not frappe.db.exists("Batch", batch_name):
            frappe.get_doc({"doctype": "Batch", "batch_id": batch_name, "item": item_code}).insert(
                ignore_permissions=True)
        row["batch_no"] = batch_name
        row["use_serial_batch_fields"] = 1

    with system_permissions():
        entry = frappe.get_doc({
            "doctype": "Stock Entry",
            "stock_entry_type": "Material Receipt",
            "company": frappe.defaults.get_global_default("company"),
            "items": [row],
        })
        entry.insert(ignore_permissions=True)
        entry.submit()
    return {"stock_entry": entry.name}


@frappe.whitelist(methods=["POST"])
def tag_warehouse_for_brand(warehouse, brand_tag):
    """Set which brand a warehouse sells for. Only a System Manager may do this."""
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only a System Manager can change which brand a warehouse sells for."),
                     frappe.PermissionError)
    frappe.db.set_value("Warehouse", warehouse, "brand_tag", brand_tag or None)
    return {"warehouse": warehouse, "brand_tag": brand_tag or None}
