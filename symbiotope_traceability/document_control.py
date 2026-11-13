"""Document control: frozen records, signed decisions, and the library.

GMP turns a record into evidence by making it impossible to change quietly.
Three things do that here, and they hold however a record is reached -- the
Console, Desk, REST or a script:

* **Frozen documents.** The moment an extraction run is completed, the SOP it
  followed and the form it was filled in against are frozen. That is the point
  of use: the document a run cites must be the document that was in force when
  it ran. A revision is a new SOP that points at the old one, never an edit.

* **Signed decisions.** Releasing or rejecting a lot, and approving or
  superseding a SOP, require the person's own password and a reason. The
  signature is appended to a log with no write or delete permission for anyone,
  not even an administrator. A click is attributed to whoever holds the
  session; a password is evidence that the person was there.

* **Audit trail.** `track_changes` is switched on for every operational doctype,
  so Frappe writes a Version row -- user, timestamp, before and after -- for
  each field change. The records that must survive are also refused deletion in
  code, because a Doctype permission is a convenience and a hook is a rule.

What software cannot do is the rest of GMP: validation of this system, training,
periodic access review, backups and disaster recovery, and a quality manual
that says these are the rules. Those are organisational, and the Console is
only as compliant as the site it runs on. This module is the part that can be
enforced by the code itself.
"""

import os

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cstr, now_datetime, today
from frappe.utils.password import check_password

from symbiotope_traceability import sop_forms
from symbiotope_traceability.api import CONSOLE_ROLES

ADMIN_ROLES = frozenset({"Symbiotope Admin", "System Manager"})
QA_ROLES = frozenset({"QA", "Symbiotope Admin", "System Manager"})


# ---------------------------------------------------------------------------
# Setup (after_migrate)
# ---------------------------------------------------------------------------


def setup_document_control():
    """Create the two signature logs, the control fields and the audit trail.

    Idempotent, because it runs after every migrate on every site the app is
    installed on.
    """
    sop_forms._ensure_doctype(
        "SOP Approval",
        [
            {"fieldname": "sop", "fieldtype": "Link", "options": "SOP", "label": "SOP",
             "reqd": 1, "in_list_view": 1, "search_index": 1},
            {"fieldname": "sop_version", "fieldtype": "Data", "label": "SOP Version",
             "in_list_view": 1, "read_only": 1},
            {"fieldname": "decision", "fieldtype": "Select", "label": "Decision",
             "options": "Approved\nSuperseded", "in_list_view": 1, "read_only": 1},
            {"fieldname": "effective_date", "fieldtype": "Date", "label": "Effective Date",
             "read_only": 1},
            {"fieldname": "signed_by", "fieldtype": "Link", "options": "User",
             "label": "Signed By", "in_list_view": 1, "read_only": 1},
            {"fieldname": "signed_on", "fieldtype": "Datetime", "label": "Signed On",
             "in_list_view": 1, "read_only": 1},
            {"fieldname": "section_break_reason", "fieldtype": "Section Break"},
            {"fieldname": "reason", "fieldtype": "Small Text", "label": "Reason",
             "reqd": 1, "read_only": 1},
        ],
        permissions=[
            ("Symbiotope Admin", "read"),
            ("System Manager", "read"),
            ("QA", "read"),
        ],
        autoname="format:SOPA-{YYYY}-{#####}",
    )

    sop_forms._ensure_doctype(
        "Lot QA Decision",
        [
            {"fieldname": "lot", "fieldtype": "Link", "options": "Batch", "label": "Lot",
             "reqd": 1, "in_list_view": 1, "search_index": 1},
            {"fieldname": "decision", "fieldtype": "Select", "label": "Decision",
             "options": "Released\nRejected", "in_list_view": 1, "read_only": 1},
            {"fieldname": "previous_status", "fieldtype": "Data", "label": "Previous Status",
             "read_only": 1},
            {"fieldname": "signed_by", "fieldtype": "Link", "options": "User",
             "label": "Signed By", "in_list_view": 1, "read_only": 1},
            {"fieldname": "signed_on", "fieldtype": "Datetime", "label": "Signed On",
             "in_list_view": 1, "read_only": 1},
            {"fieldname": "section_break_reason", "fieldtype": "Section Break"},
            {"fieldname": "reason", "fieldtype": "Small Text", "label": "Reason",
             "reqd": 1, "read_only": 1},
        ],
        permissions=[
            ("Symbiotope Admin", "read"),
            ("System Manager", "read"),
            ("QA", "read"),
            ("Production Operator", "read"),
            ("Grower Harvester", "read"),
            ("Stock Manager", "read"),
        ],
        autoname="format:QAD-{YYYY}-{#####}",
    )

    create_custom_fields(
        {
            "SOP": [
                {
                    "fieldname": "document_control_section",
                    "fieldtype": "Section Break",
                    "label": "Document Control",
                    "insert_after": "effective_date",
                },
                {
                    "fieldname": "approved_by",
                    "fieldtype": "Link",
                    "options": "User",
                    "label": "Approved By",
                    "insert_after": "document_control_section",
                    "read_only": 1,
                },
                {
                    "fieldname": "approved_on",
                    "fieldtype": "Datetime",
                    "label": "Approved On",
                    "insert_after": "approved_by",
                    "read_only": 1,
                },
                {
                    "fieldname": "frozen_on",
                    "fieldtype": "Datetime",
                    "label": "Frozen On",
                    "insert_after": "approved_on",
                    "read_only": 1,
                    "description": "Set automatically when a run citing this SOP is completed. From then on the SOP is a record and cannot be edited.",
                },
                {
                    "fieldname": "frozen_by",
                    "fieldtype": "Link",
                    "options": "User",
                    "label": "Frozen By",
                    "insert_after": "frozen_on",
                    "read_only": 1,
                },
                {
                    "fieldname": "previous_version",
                    "fieldtype": "Link",
                    "options": "SOP",
                    "label": "Previous Version",
                    "insert_after": "frozen_by",
                    "read_only": 1,
                    "description": "The version this one revises. Set when a new version is issued.",
                },
                {
                    "fieldname": "superseded_by",
                    "fieldtype": "Link",
                    "options": "SOP",
                    "label": "Superseded By",
                    "insert_after": "previous_version",
                    "read_only": 1,
                },
                {
                    "fieldname": "change_reason",
                    "fieldtype": "Small Text",
                    "label": "Reason for Revision",
                    "insert_after": "superseded_by",
                    "description": "Why this version exists. Recorded on the revision, not on the version it replaces.",
                },
            ],
            "Extraction Batch": [
                {
                    "fieldname": "completed_on",
                    "fieldtype": "Datetime",
                    "label": "Completed On",
                    "insert_after": "status",
                    "read_only": 1,
                    "description": "When the run was closed. A completed batch record is not editable.",
                },
                {
                    "fieldname": "completed_by",
                    "fieldtype": "Link",
                    "options": "User",
                    "label": "Completed By",
                    "insert_after": "completed_on",
                    "read_only": 1,
                },
            ],
            "Batch": [
                {
                    "fieldname": "released_on",
                    "fieldtype": "Datetime",
                    "label": "Released On",
                    "insert_after": "item",
                    "read_only": 1,
                },
                {
                    "fieldname": "released_by",
                    "fieldtype": "Link",
                    "options": "User",
                    "label": "Released By",
                    "insert_after": "released_on",
                    "read_only": 1,
                },
            ],
        },
        update=True,
    )

    _track_changes(
        [
            "SOP",
            "SOP Form Template",
            "Extraction Batch",
            "Harvest Batch",
            "Batch",
            "Plant Source",
            "Processing Stage Log",
        ]
    )


def _track_changes(doctypes):
    """Turn on Frappe's Version trail for the records GMP asks about.

    `track_changes` is a DocType property, not a field, so it is set on the
    DocType record itself. Once on, every save writes a Version row naming the
    user, the time, and the fields that changed -- the "who changed what" an
    auditor opens the record to find.
    """
    for doctype in doctypes:
        if not frappe.db.exists("DocType", doctype):
            continue
        if frappe.db.get_value("DocType", doctype, "track_changes"):
            continue
        frappe.db.set_value("DocType", doctype, "track_changes", 1)
        frappe.clear_cache(doctype=doctype)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def on_sop_trash(doc, method=None):
    """An approved, published or run SOP is a record, not a draft file.

    Only a Draft that was never approved can be removed -- a mistyped upload
    should not have to live forever. An approved version is evidence even
    before anything ran against it, and a superseded one is what runs in the
    past were written against, so both stay.
    """
    if doc.status != "Draft" or doc.get("approved_on"):
        frappe.throw(
            f"SOP {doc.name} is {doc.status} and has been approved, so it is a "
            f"controlled record and cannot be deleted. Supersede it or issue a "
            f"new version instead."
        )
    if doc.get("frozen_on"):
        frappe.throw(
            f"SOP {doc.name} is frozen: a completed extraction run cites it. "
            f"It cannot be deleted. Issue a new version if the procedure changed."
        )
    if frappe.db.exists("Extraction Batch", {"sop": doc.name}):
        frappe.throw(
            f"SOP {doc.name} has extraction runs citing it and cannot be deleted."
        )
    if frappe.db.exists("SOP Form Template", {"sop": doc.name, "status": "Published"}):
        frappe.throw(
            f"SOP {doc.name} has a published extraction form and cannot be "
            f"deleted. A published form makes it a controlled document: approve "
            f"and supersede it rather than deleting it."
        )

    # Only an unused draft gets this far; take its unapproved form and its
    # document with it, so no orphan record is left pointing at a SOP that no
    # longer exists. The flag is what lets the File guard below tell an
    # intentional removal from a controller reaching for the evidence.
    frappe.flags.setdefault("sops_being_deleted", set()).add(doc.name)
    for name in frappe.get_all(
        "SOP Form Template", filters={"sop": doc.name}, pluck="name"
    ):
        frappe.delete_doc(
            "SOP Form Template", name, ignore_permissions=True, force=True
        )
    files = set(
        frappe.get_all(
            "File",
            filters={"attached_to_doctype": "SOP", "attached_to_name": doc.name},
            pluck="name",
        )
    )
    if doc.get("document"):
        # The Console uploads the document before the SOP exists, so that File
        # has no attached_to set; match it by URL or it stays behind orphaned.
        # A revision shares its source's file, so a file another SOP still
        # points at stays: deleting it would take their document with it.
        if not frappe.db.exists(
            "SOP", {"document": doc.document, "name": ("!=", doc.name)}
        ):
            files.update(
                frappe.get_all(
                    "File", filters={"file_url": doc.document}, pluck="name"
                )
            )
    for name in files:
        frappe.delete_doc("File", name, ignore_permissions=True, force=True)


def on_form_before_save(doc, method=None):
    """A published form is the record of what runs were filled in against.

    Editing it would retroactively change the questions an operator already
    answered, so it is refused at the doctype, not only in the Console's save
    method. A revision is a new template, published over the old one.
    """
    previous = doc.get_doc_before_save()
    if previous and previous.status == "Published":
        frappe.throw(
            f"Form {doc.name} is published and cannot be edited. A revised form "
            f"is a new template; publishing it supersedes this one."
        )

    if doc.sop:
        frozen_on = frappe.db.get_value("SOP", doc.sop, "frozen_on")
        if frozen_on:
            frappe.throw(
                f"SOP {doc.sop} was frozen on {frozen_on}, so its extraction "
                f"form cannot be changed or published. Issue a new SOP version."
            )
        if frappe.db.get_value("SOP", doc.sop, "status") == "Superseded":
            frappe.throw(
                f"SOP {doc.sop} is superseded; its form is a record and cannot "
                f"be changed or published."
            )


def on_form_trash(doc, method=None):
    if doc.status == "Published":
        frappe.throw(
            f"Form {doc.name} is published and cannot be deleted: it is the "
            f"form extraction runs were filled in against."
        )
    if frappe.db.exists("Extraction Batch", {"form_template": doc.name}):
        frappe.throw(
            f"Form {doc.name} is attached to extraction runs and cannot be deleted."
        )


def on_extraction_before_save(doc, method=None):
    """A completed run is a closed record, and its closure is stamped.

    Runs must run before the automation's own before_save, so a closed record is
    refused before any stock could move.
    """
    previous = doc.get_doc_before_save()
    if previous and previous.status == "Complete" and doc.is_dirty():
        frappe.throw(
            f"Extraction {doc.name} is complete. Its batch record is closed and "
            f"cannot be changed; a correction is a deviation, not an edit."
        )

    # Work is done to a procedure in force, and to one procedure only. A new
    # run may only cite an Active version, and a run that already cites one
    # cannot swap it: the form on the record was snapshotted from the SOP it
    # started with, so a swap would leave the batch record claiming a document
    # it was never filled in against.
    if doc.sop and doc.has_value_changed("sop"):
        if previous and previous.get("sop"):
            frappe.throw(
                f"Extraction {doc.name} was started against SOP {previous.sop} "
                f"and its form was taken from it. The SOP cannot be changed; "
                f"start a new run instead."
            )
        status = frappe.db.get_value("SOP", doc.sop, "status")
        if status != "Active":
            frappe.throw(
                f"SOP {doc.sop} is {status}. A run can only be started against "
                f"the version in force; approve it first, or pick another SOP."
            )

    if doc.status == "Complete" and (not previous or previous.status != "Complete"):
        doc.completed_on = now_datetime()
        doc.completed_by = frappe.session.user


def on_extraction_updated(doc, method=None):
    """Freeze the SOP and its form the moment a run is completed.

    Done after the save, because only a completed save has actually consumed
    the inputs and produced the extract lot; a run that failed part-way through
    must not freeze the document it was working to.
    """
    if doc.status != "Complete" or not doc.sop:
        return
    _freeze_sop(doc.sop, doc.name)


def on_extraction_trash(doc, method=None):
    """A run that has started has consumed material; only an untouched draft
    can be removed."""
    if doc.status != "Draft":
        frappe.throw(
            f"Extraction {doc.name} is {doc.status} and is a production record. "
            f"It cannot be deleted."
        )


def on_harvest_trash(doc, method=None):
    if (
        doc.get("raw_material_batch")
        or frappe.db.exists("Processing Stage Log", {"harvest_batch": doc.name})
        or frappe.db.exists("Batch", {"harvest_batch": doc.name})
    ):
        frappe.throw(
            f"Harvest {doc.name} has processing records or a raw-material lot "
            f"and cannot be deleted."
        )


def on_stage_log_trash(doc, method=None):
    frappe.throw(
        "Processing stage logs are production records and cannot be deleted. "
        "A correction is a new log, not the removal of the old one."
    )


def on_lot_before_save(doc, method=None):
    """A lot's QA status changes only through a signed decision."""
    if doc.is_new() or not doc.has_value_changed("lot_status"):
        return
    if frappe.flags.get("lot_signature"):
        return
    frappe.throw(
        f"Lot {doc.name}'s status changes only through a signed QA decision. "
        f"Use Release or Reject on the lot's page, which asks for your password "
        f"and a reason."
    )


def on_lot_trash(doc, method=None):
    frappe.throw(
        f"Lot {doc.name} is a stock and traceability record and cannot be "
        f"deleted. Reject it instead."
    )


def on_plant_source_trash(doc, method=None):
    if frappe.db.exists("Batch", {"plant_source": doc.name}) or frappe.db.exists(
        "Harvest Batch", {"plant_source": doc.name}
    ):
        frappe.throw(
            f"Plant Source {doc.name} has lots or harvests recorded against it "
            f"and cannot be deleted."
        )


def on_file_trash(doc, method=None):
    """The SOP's document is part of the controlled record.

    A file is matched to its SOP by URL as well as by attachment, because the
    Console uploads the document before the SOP exists and so the File has no
    `attached_to` set. Either way, the bytes behind a controlled procedure are
    not something to delete from the file list. Allowed only while the SOP
    holding it is itself a deletable draft being removed.
    """
    holders = []
    if doc.file_url:
        holders = frappe.get_all(
            "SOP", filters={"document": doc.file_url}, pluck="name"
        )
    if (
        doc.attached_to_doctype == "SOP"
        and doc.attached_to_name
        and frappe.db.exists("SOP", doc.attached_to_name)
        and doc.attached_to_name not in holders
    ):
        holders.append(doc.attached_to_name)
    if not holders:
        return

    being_deleted = frappe.flags.get("sops_being_deleted", set())
    if all(name in being_deleted for name in holders):
        return

    frappe.throw(
        f"{doc.file_name or 'This file'} is the document of SOP "
        f"{', '.join(holders)} and cannot be deleted. It is the controlled "
        f"record of that procedure."
    )


def on_signature_trash(doc, method=None):
    """A signature is the evidence that a named person made a decision.

    It is append-only by permission already -- no role is granted write or
    delete on either log -- but an Administrator bypasses DocPerm, so deletion
    is refused here as well. An audit trail with a gap in it is the one thing a
    record cannot have.
    """
    frappe.throw(
        f"{doc.doctype} {doc.name} is a signature record and cannot be deleted. "
        f"It is the evidence that the decision was made by the person named on it."
    )


def _apply_sop_status(name, values):
    """Change a SOP's control fields as a save, not a silent db write.

    `db_set` would be shorter, but it skips the Version row, and a status
    change that leaves no trace in the record's own history is exactly what
    document control is supposed to prevent.

    Two flags tell the SOP's before_save hook what it is looking at:
    `sop_control_write` marks a write of control fields by this module, so the
    freeze's field-level refusal does not catch the approval that puts a
    revision in force; `sop_signature` marks the status change itself as coming
    from a signature, which is the only way status may move.
    """
    doc = frappe.get_doc("SOP", name)
    for fieldname, value in values.items():
        doc.set(fieldname, value)
    previous_signature = frappe.flags.get("sop_signature")
    # Both flags are restored rather than cleared, so a nested write cannot
    # leave a later save believing it is something it is not.
    frappe.flags.sop_control_write = True
    frappe.flags.sop_signature = bool(previous_signature) or "status" in values
    try:
        doc.save(ignore_permissions=True)
    finally:
        frappe.flags.sop_control_write = False
        frappe.flags.sop_signature = previous_signature


def _freeze_sop(sop, run):
    if frappe.db.get_value("SOP", sop, "frozen_on"):
        return
    _apply_sop_status(
        sop, {"frozen_on": now_datetime(), "frozen_by": frappe.session.user}
    )
    frappe.logger("symbiotope").info(
        f"SOP {sop} frozen by completion of extraction {run}."
    )


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def sign_sop(sop, decision, reason, password):
    """Approve a SOP into force, or supersede one that is in force.

    Two decisions, both signed. Approving a revision also supersedes the
    version it was raised against, so exactly one version is ever Active.
    """
    _require_admin()
    doc = frappe.get_doc("SOP", sop)
    decision = cstr(decision).strip()
    if decision not in ("Approved", "Superseded"):
        frappe.throw("A SOP is either Approved or Superseded; that is all a signature decides.")

    reason = _reason(reason)
    effective = None

    if decision == "Approved":
        if doc.status == "Active":
            frappe.throw(f"SOP {doc.name} is already Active.")
        if doc.status == "Superseded":
            frappe.throw(
                f"SOP {doc.name} is superseded. Raise a new version from it "
                f"instead of reactivating the old one."
            )
        if not doc.document:
            frappe.throw(
                f"SOP {doc.name} has no document attached, so there is nothing "
                f"to approve."
            )
        _verify_password(password)
        effective = doc.effective_date or today()
        _apply_sop_status(
            doc.name,
            {
                "status": "Active",
                "approved_by": frappe.session.user,
                "approved_on": now_datetime(),
                "effective_date": effective,
            },
        )
        if doc.get("previous_version"):
            _apply_sop_status(
                doc.previous_version,
                {"status": "Superseded", "superseded_by": doc.name},
            )
    else:
        if doc.status != "Active":
            frappe.throw(
                f"SOP {doc.name} is {doc.status}, so there is nothing in force "
                f"to supersede."
            )
        _verify_password(password)
        _apply_sop_status(doc.name, {"status": "Superseded"})

    log = frappe.get_doc(
        {
            "doctype": "SOP Approval",
            "sop": doc.name,
            "sop_version": doc.version,
            "decision": decision,
            "effective_date": effective,
            "signed_by": frappe.session.user,
            "signed_on": now_datetime(),
            "reason": reason,
        }
    )
    log.insert(ignore_permissions=True)

    return {
        "sop": doc.name,
        "status": frappe.db.get_value("SOP", doc.name, "status"),
        "approval": log.name,
    }


@frappe.whitelist(methods=["POST"])
def sign_lot(lot, decision, reason, password):
    """Release or reject a lot, signed by the person deciding.

    The status on the lot and the decision log are written together: a release
    with no matching signed row is exactly what a QA record must not contain.
    """
    _require_qa()
    doc = frappe.get_doc("Batch", lot)
    doc.check_permission("write")

    decision = cstr(decision).strip()
    if decision not in ("Released", "Rejected"):
        frappe.throw("A lot is either Released or Rejected.")

    previous = doc.get("lot_status")
    if previous == decision:
        frappe.throw(f"Lot {doc.name} is already {decision}.")
    if previous != "Quarantine":
        frappe.throw(
            f"Lot {doc.name} is {previous}; only a lot in Quarantine can be "
            f"released or rejected."
        )

    reason = _reason(reason)
    _verify_password(password)

    doc.lot_status = decision
    doc.released_by = frappe.session.user
    doc.released_on = now_datetime()
    frappe.flags.lot_signature = True
    try:
        doc.save(ignore_permissions=True)
    finally:
        frappe.flags.lot_signature = False

    log = frappe.get_doc(
        {
            "doctype": "Lot QA Decision",
            "lot": doc.name,
            "decision": decision,
            "previous_status": previous,
            "signed_by": frappe.session.user,
            "signed_on": now_datetime(),
            "reason": reason,
        }
    )
    log.insert(ignore_permissions=True)

    return {
        "lot": doc.name,
        "lot_status": decision,
        "decision": log.name,
    }


def _verify_password(password):
    password = cstr(password)
    if not password:
        frappe.throw("Re-enter your password to sign. Nothing was recorded.")
    try:
        check_password(frappe.session.user, password)
    except frappe.AuthenticationError:
        frappe.throw(
            "That password is not correct, so nothing was signed.",
            frappe.ValidationError,
        )


def _reason(reason):
    reason = cstr(reason).strip()
    if len(reason) < 10:
        frappe.throw(
            "Give a reason of at least ten characters. It is part of the "
            "signature, and it is what a deviation report is written from."
        )
    return reason


def _require_admin():
    if not ADMIN_ROLES.intersection(frappe.get_roles()):
        frappe.throw(
            "Not permitted: only an admin may approve or supersede a SOP.",
            frappe.PermissionError,
        )


def _require_qa():
    if not QA_ROLES.intersection(frappe.get_roles()):
        frappe.throw(
            "Not permitted: only QA may release or reject a lot.",
            frappe.PermissionError,
        )


# ---------------------------------------------------------------------------
# Issues a revision
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def new_sop_version(sop, sop_id, version, reason=None):
    """Create the next version as a new SOP, linked to the one it revises.

    A revision is a separate record because the old one is evidence: every run
    that cites it keeps citing it, with the version it ran to. Changing the old
    record instead would rewrite those runs' history.

    The new SOP starts as a Draft with the same document and title. Its form is
    parsed fresh (`on_sop_created`), because the document may have changed --
    and if it has not, the parse reproduces the same questions for review.
    """
    _require_admin()
    old = frappe.get_doc("SOP", sop)
    if old.status == "Draft":
        frappe.throw(
            f"SOP {old.name} is still a Draft. Edit it, or approve it first; a "
            f"revision is only meaningful once a version is in force."
        )

    sop_id = cstr(sop_id).strip()
    version = cstr(version).strip()
    if not sop_id or not version:
        frappe.throw("A new version needs its own SOP ID and version.")
    if sop_id == old.sop_id:
        frappe.throw(
            f"{sop_id} is already the ID of {old.name}. A version's ID must be "
            f"distinct; try {old.sop_id}-V{version}."
        )
    if frappe.db.exists("SOP", sop_id):
        frappe.throw(f"SOP {sop_id} already exists.")
    if version == old.version:
        frappe.throw(
            f"{version} is the version already in force. A revision needs a new "
            f"version number."
        )
    if not old.document:
        frappe.throw(
            f"SOP {old.name} has no document attached, so there is nothing to "
            f"revise."
        )

    new = frappe.get_doc(
        {
            "doctype": "SOP",
            "sop_id": sop_id,
            "title": old.title,
            "version": version,
            "status": "Draft",
            "document": old.document,
            "notes": old.notes,
            "previous_version": old.name,
            "change_reason": cstr(reason).strip() or None,
        }
    )
    new.insert(ignore_permissions=True)

    return {"sop": new.name, "version": version, "previous_version": old.name}


# ---------------------------------------------------------------------------
# The document library
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_document_library():
    """Every controlled document, and every completed run, for download.

    SOPs are read through `frappe.get_list`, so Frappe's own permissions decide
    what a caller sees: an operational role sees the library, a client sees only
    the procedures it uploaded. Completed runs are operational records and are
    returned only to a Console role.
    """
    roles = frappe.get_roles()
    can_see_runs = bool(CONSOLE_ROLES.intersection(roles))

    sops = []
    if frappe.has_permission("SOP", "read"):
        sops = frappe.get_list(
            "SOP",
            fields=[
                "name",
                "sop_id",
                "title",
                "version",
                "status",
                "effective_date",
                "document",
                "frozen_on",
                "frozen_by",
                "approved_on",
                "approved_by",
                "previous_version",
                "superseded_by",
                "change_reason",
            ],
            order_by="title asc, version asc",
            limit_page_length=0,
        )

    names = [sop["name"] for sop in sops]
    forms = {}
    if names:
        for form in frappe.get_all(
            "SOP Form Template",
            filters={"sop": ("in", names), "status": "Published"},
            fields=["name", "sop", "published_on", "field_count"],
        ):
            forms[form.sop] = form

    for sop in sops:
        form = forms.get(sop.name)
        sop["has_document"] = bool(sop.pop("document", None))
        sop["form_template"] = form.name if form else None
        sop["form_published_on"] = form.published_on if form else None
        sop["form_fields"] = form.field_count if form else 0
        sop["completed_runs"] = (
            frappe.db.count(
                "Extraction Batch", {"sop": sop.name, "status": "Complete"}
            )
            if can_see_runs
            else 0
        )

    runs = []
    if can_see_runs:
        runs = frappe.get_list(
            "Extraction Batch",
            filters={"status": "Complete"},
            fields=[
                "name",
                "sop",
                "sop_version_snapshot",
                "operator",
                "date_time",
                "completed_on",
                "completed_by",
                "output_extract_item",
                "output_extract_batch",
                "form_template",
            ],
            order_by="date_time desc",
            limit_page_length=0,
        )
        for run in runs:
            run["sop_version"] = run.pop("sop_version_snapshot", None)

    return {"sops": sops, "runs": runs}


@frappe.whitelist()
def download_sop_document(sop):
    """Serve the SOP's own document as an attachment.

    The file is sent through this method rather than linked at its file URL so
    the read is checked against the SOP: a private attachment URL alone would
    be a guessable path with no record-level permission behind it.
    """
    doc = frappe.get_doc("SOP", sop)
    doc.check_permission("read")
    if not doc.document:
        frappe.throw(f"SOP {doc.name} has no document attached.")

    files = frappe.get_all(
        "File", filters={"file_url": doc.document}, fields=["name", "file_name"], limit=1
    )
    if not files:
        frappe.throw(f"The document {doc.document} is not in the file store.")

    file_doc = frappe.get_doc("File", files[0].name)
    path = file_doc.get_full_path()
    if not os.path.exists(path):
        frappe.throw(f"The document {file_doc.file_name} is missing from the file store.")

    with open(path, "rb") as handle:
        content = handle.read()

    extension = os.path.splitext(path)[1]
    filename = cstr(file_doc.file_name or f"{doc.name}{extension}").replace('"', "")
    frappe.local.response.filename = filename
    frappe.local.response.filecontent = content
    frappe.local.response.type = "download"


@frappe.whitelist()
def get_sop_approvals(sop):
    doc = frappe.get_doc("SOP", sop)
    doc.check_permission("read")
    return frappe.get_all(
        "SOP Approval",
        filters={"sop": sop},
        fields=["name", "decision", "sop_version", "reason", "signed_by", "signed_on", "effective_date"],
        order_by="signed_on desc",
    )


@frappe.whitelist()
def get_lot_decisions(lot):
    doc = frappe.get_doc("Batch", lot)
    doc.check_permission("read")
    return frappe.get_all(
        "Lot QA Decision",
        filters={"lot": lot},
        fields=["name", "decision", "previous_status", "reason", "signed_by", "signed_on"],
        order_by="signed_on desc",
    )
