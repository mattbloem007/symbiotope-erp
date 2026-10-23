"""Whitelisted endpoints for the Symbiotope Console frontend.

Everything here is UX-only: it tells the frontend which nav items/actions to
show for the logged-in user. It is never the thing enforcing permissions --
every actual read/write still goes through Frappe's own Role/DocPerm checks
on the resource it targets, exactly as if the request came from Desk.
"""

import frappe


@frappe.whitelist()
def get_my_context():
    user = frappe.session.user
    return {
        "email": user,
        "full_name": frappe.db.get_value("User", user, "full_name") or user,
        "roles": frappe.get_roles(user),
    }
