"""SOP-driven extraction forms.

A batch record is not a bag of inputs: the SOP decides which values an operator
records, in which order, with which specification beside them. Different client
SOPs therefore produce different extraction forms, and the form a run is filled
in against has to be the form of the SOP that run actually followed.

So the form is generated from the SOP document rather than written in code:

    SOP (client uploads the .docx)
        -> parse            -> a Draft template (see sop_form_parser)
            -> admin review  -> corrections in the Console
                -> Publish   -> SOP Form Template (status Published)
                    -> Extraction Batch snapshots its rows
                        -> the Console renders them; completion is gated on
                           the required ones

Why the answers are snapshotted rather than linked
--------------------------------------------------
`Extraction Batch Form Value` copies the label, type, unit, specification and
required flag of every field onto the run at the moment the SOP is attached,
and only then stores the operator's answer beside it. A published form is never
edited -- revising it makes a new template -- but a snapshot means even that
rule can be broken later without a run's record changing underneath it. It is
the same reasoning as `sop_version_snapshot`: the batch record must say what the
operator was actually asked, not what today's SOP happens to ask.

Why publishing needs a human
----------------------------
`sop_form_parser` reads the house style -- numbered ALL-CAPS sections, tables
whose first row names the columns, tick sequences -- and gets most of a document
of this family right. What it cannot know is which column is the rule and which
is what the operator writes, which rows are a repeat block, or that
"Goedgekeurde grondstof: Nee" must send someone to a deviation report. A wrong
guess there yields a complete-looking batch record that is not what the SOP
says, which is the worst possible failure for a QC document. Hence: draft,
review, publish.
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cstr, now_datetime

from symbiotope_traceability.api import CONSOLE_ROLES
from symbiotope_traceability.sop_form_parser import field_key, parse_docx

ADMIN_ROLES = frozenset({"Symbiotope Admin", "System Manager"})

#: Who may add a SOP. Clients hold this and nothing else; a SOP they add stays a
#: Draft until an admin activates it, so an uploaded file cannot by itself
#: change what a controlled extraction is allowed to do.
SUBMITTER_ROLE = "SOP Submitter"

FIELD_TYPES = (
    "text",
    "long_text",
    "number",
    "percent",
    "date",
    "time",
    "datetime",
    "select",
    "checkbox",
    "checkbox_group",
    "signature",
)
BLOCK_TYPES = ("fields", "table", "calc", "doc_control")
ROLES = ("input", "result", "data")
#: Types whose answer is typed, as opposed to ticked. Everything typed is
#: expected to be filled in; ticks default to optional because a checklist row
#: that never applied to this run should not read as a missing value.
TYPED = frozenset(
    {"text", "long_text", "number", "percent", "date", "time", "datetime", "select", "signature"}
)

#: Fields a frozen SOP may still carry a change on. These are the control
#: fields a signed decision writes: superseding a frozen SOP is legitimate --
#: the version in force moves on, while the frozen record itself stays as it
#: was. Everything else is refused once a completed run cites the SOP.
FROZEN_ALLOWED = frozenset(
    {
        "status",
        "superseded_by",
        "approved_by",
        "approved_on",
        "previous_version",
        "modified",
        "modified_by",
    }
)


def _sop_frozen(sop):
    """When a SOP was frozen by a completed run, else None.

    Read from the database rather than from a loaded document: a SOP object
    fetched before another request completed a run would otherwise still look
    editable, and document control is only worth anything if it is decided
    against the stored record.
    """
    if not sop:
        return None
    return frappe.db.get_value("SOP", sop, "frozen_on")


def _refuse_frozen(sop, what):
    frozen_on = _sop_frozen(sop)
    if frozen_on:
        frappe.throw(
            f"SOP {sop} was frozen on {frozen_on} because a completed extraction "
            f"run cites it, so its {what} cannot be changed. Issue a new version: "
            f"a revision is a new SOP, not an edit to the record."
        )


# ---------------------------------------------------------------------------
# Setup (after_migrate: the doctypes and fields must exist on a fresh site)
# ---------------------------------------------------------------------------


def setup_sop_forms():
    _ensure_role(SUBMITTER_ROLE)

    _ensure_doctype("SOP Form Field", _template_field_columns(), istable=True)
    _ensure_doctype(
        "SOP Form Template",
        _template_columns(),
        permissions=[
            ("Symbiotope Admin", "read,write,create,delete"),
            ("System Manager", "read,write,create,delete"),
            # Read-only for the roles that run to a form or audit one.
            ("QA", "read"),
            ("Production Operator", "read"),
            ("Grower Harvester", "read"),
            ("Stock Manager", "read"),
        ],
    )
    _ensure_doctype("Extraction Batch Form Value", _value_columns(), istable=True)

    create_custom_fields(
        {
            "Extraction Batch": [
                {
                    "fieldname": "form_template",
                    "fieldtype": "Link",
                    "options": "SOP Form Template",
                    "label": "Form Template",
                    "insert_after": "sop_version_snapshot",
                    "read_only": 1,
                    "description": "The published form of the SOP this run follows. Snapshotted, never re-linked.",
                },
                {
                    "fieldname": "form_values",
                    "fieldtype": "Table",
                    "options": "Extraction Batch Form Value",
                    "label": "Batch Record",
                    "insert_after": "form_template",
                    "description": "One row per value the SOP asks for, copied from the form when the SOP is attached.",
                },
                {
                    "fieldname": "form_override_reason",
                    "fieldtype": "Small Text",
                    "label": "Completion Override Reason",
                    "insert_after": "observations",
                    "description": "Setting this lets an admin complete a run with required fields missing. The reason, the user and the time are recorded on the batch.",
                },
                {
                    "fieldname": "form_override_by",
                    "fieldtype": "Link",
                    "options": "User",
                    "label": "Overridden By",
                    "insert_after": "form_override_reason",
                    "read_only": 1,
                },
                {
                    "fieldname": "form_override_on",
                    "fieldtype": "Datetime",
                    "label": "Overridden On",
                    "insert_after": "form_override_by",
                    "read_only": 1,
                },
            ]
        },
        update=True,
    )

    _let_submitters_add_sops()

    _draft_missing_forms()


def _draft_missing_forms():
    """Read a form for any SOP that gained a document before this hook existed.

    Uploading a document is meant to produce a draft on its own; on a site that
    already held SOPs before that behaviour shipped, the draft would otherwise
    wait for someone to press a button they do not know is there. Only SOPs
    with no form at all are touched -- a reviewed draft or a published form is
    never replaced by a deploy.
    """
    if not frappe.db.exists("DocType", "SOP"):
        return
    for name in frappe.get_all(
        "SOP",
        filters=[["document", "is", "set"], ["status", "in", ["Draft", "Active"]]],
        pluck="name",
    ):
        doc = frappe.get_doc("SOP", name)
        if doc.get("frozen_on"):
            continue
        if frappe.db.exists("SOP Form Template", {"sop": name}):
            continue
        _draft_quietly(doc)


def _ensure_role(role):
    if frappe.db.exists("Role", role):
        return
    frappe.get_doc(
        {"doctype": "Role", "role_name": role, "desk_access": 1}
    ).insert(ignore_permissions=True)


def _ensure_doctype(
    name,
    fields,
    istable=False,
    permissions=None,
    autoname="format:SOPF-{YYYY}-{#####}",
):
    """Create the doctype if the site does not have it yet.

    These are site-local doctypes, the same way SOP and Extraction Batch were
    made here, rather than JSON files shipped in the app: the app is installed
    on one company's site, and a doctype that exists only after `after_migrate`
    keeps the patterns they already use. Idempotent, so every deploy is safe.
    """
    if frappe.db.exists("DocType", name):
        return

    doc = frappe.get_doc(
        {
            "doctype": "DocType",
            "name": name,
            "module": "Custom",
            "custom": 1,
            "istable": 1 if istable else 0,
            "editable_grid": 1 if istable else 0,
            "engine": "InnoDB",
            "allow_rename": 0 if istable else 1,
            "sort_field": "modified",
            "sort_order": "DESC",
            "track_changes": 0 if istable else 1,
            # A custom doctype with a series needs this named explicitly; the
            # Console prints a form code off it, so it is not just an internal id.
            "naming_rule": None if istable else "Expression (old style)",
            "autoname": None if istable else autoname,
            "fields": fields,
            "permissions": [] if istable else [
                {"role": role, "permlevel": 0, "read": 1,
                 **{p.strip(): 1 for p in ptypes.split(",")}}
                for role, ptypes in (permissions or [])
            ],
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.clear_cache(doctype=name)


def _template_columns():
    return [
        {"fieldname": "sop", "fieldtype": "Link", "options": "SOP", "label": "SOP",
         "reqd": 1, "in_list_view": 1, "search_index": 1},
        {"fieldname": "sop_version", "fieldtype": "Data", "label": "SOP Version",
         "in_list_view": 1, "read_only": 1},
        {"fieldname": "title", "fieldtype": "Data", "label": "Form Title", "in_list_view": 1},
        {"fieldname": "status", "fieldtype": "Select", "label": "Status", "default": "Draft",
         "options": "Draft\nPublished\nSuperseded", "in_list_view": 1,
         "in_standard_filter": 1},
        {"fieldname": "column_break_1", "fieldtype": "Column Break"},
        {"fieldname": "source_document", "fieldtype": "Data", "label": "Source Document",
         "read_only": 1},
        {"fieldname": "source_hash", "fieldtype": "Data", "label": "Source Hash",
         "read_only": 1},
        {"fieldname": "field_count", "fieldtype": "Int", "label": "Fields", "read_only": 1},
        {"fieldname": "reviewed_by", "fieldtype": "Link", "options": "User",
         "label": "Reviewed By", "read_only": 1},
        {"fieldname": "published_on", "fieldtype": "Datetime", "label": "Published On",
         "read_only": 1},
        {"fieldname": "section_break_1", "fieldtype": "Section Break"},
        {"fieldname": "review_notes", "fieldtype": "Small Text", "label": "Review Notes"},
        {"fieldname": "fields", "fieldtype": "Table", "options": "SOP Form Field",
         "label": "Fields"},
    ]


def _template_field_columns():
    return [
        {"fieldname": "sequence", "fieldtype": "Int", "label": "#", "in_list_view": 1,
         "columns": 1},
        {"fieldname": "section", "fieldtype": "Data", "label": "Section",
         "in_list_view": 1, "columns": 2},
        {"fieldname": "block_ref", "fieldtype": "Int", "label": "Block"},
        {"fieldname": "block", "fieldtype": "Data", "label": "Block Label"},
        {"fieldname": "block_type", "fieldtype": "Select", "label": "Block Type",
         "options": "\n".join(BLOCK_TYPES), "default": "fields"},
        {"fieldname": "field_key", "fieldtype": "Data", "label": "Key", "reqd": 1,
         "in_list_view": 1, "columns": 2},
        {"fieldname": "label", "fieldtype": "Data", "label": "Label", "reqd": 1,
         "in_list_view": 1, "columns": 3},
        {"fieldname": "field_type", "fieldtype": "Select", "label": "Type",
         "options": "\n".join(FIELD_TYPES), "default": "text", "in_list_view": 1,
         "columns": 2},
        {"fieldname": "options", "fieldtype": "Small Text",
         "label": "Options (one per line)"},
        {"fieldname": "unit", "fieldtype": "Data", "label": "Unit"},
        {"fieldname": "specification", "fieldtype": "Data", "label": "Specification"},
        {"fieldname": "note", "fieldtype": "Small Text", "label": "Printed Note"},
        {"fieldname": "required", "fieldtype": "Check", "label": "Required",
         "in_list_view": 1, "columns": 1},
        {"fieldname": "role", "fieldtype": "Select", "label": "Role",
         "options": "\n".join(ROLES), "default": "input"},
    ]


def _value_columns():
    return [
        {"fieldname": "sequence", "fieldtype": "Int", "label": "#", "in_list_view": 1,
         "columns": 1},
        {"fieldname": "section", "fieldtype": "Data", "label": "Section",
         "in_list_view": 1, "columns": 2},
        {"fieldname": "block_ref", "fieldtype": "Int", "label": "Block"},
        {"fieldname": "block", "fieldtype": "Data", "label": "Block Label"},
        {"fieldname": "block_type", "fieldtype": "Data", "label": "Block Type"},
        {"fieldname": "field_key", "fieldtype": "Data", "label": "Key",
         "in_list_view": 1, "columns": 2},
        {"fieldname": "label", "fieldtype": "Data", "label": "Label",
         "in_list_view": 1, "columns": 3},
        {"fieldname": "field_type", "fieldtype": "Data", "label": "Type"},
        {"fieldname": "options", "fieldtype": "Small Text",
         "label": "Options (one per line)"},
        {"fieldname": "unit", "fieldtype": "Data", "label": "Unit"},
        {"fieldname": "specification", "fieldtype": "Data", "label": "Specification"},
        {"fieldname": "note", "fieldtype": "Small Text", "label": "Printed Note"},
        {"fieldname": "required", "fieldtype": "Check", "label": "Required"},
        {"fieldname": "role", "fieldtype": "Data", "label": "Role"},
        {"fieldname": "value", "fieldtype": "Small Text", "label": "Value",
         "in_list_view": 1, "columns": 3},
    ]


def _let_submitters_add_sops():
    """A client may add a SOP -- and only see their own.

    `if_owner` is what keeps one client's procedures out of another's list; the
    submitted file is theirs, but activating it changes what the lab may do, so
    write access on someone else's SOP is exactly what this must not grant.
    """
    for ptype, owned in (("read", 1), ("write", 1), ("create", 0)):
        # Permissions are boolean columns, not rows with a type.
        if frappe.db.exists(
            "Custom DocPerm",
            {"parent": "SOP", "role": SUBMITTER_ROLE, "permlevel": 0, ptype: 1},
        ):
            continue
        _grant("SOP", SUBMITTER_ROLE, ptype, if_owner=owned)


def _grant(doctype, role, ptype, if_owner=0):
    """Add one Custom DocPerm row, tolerating the signature difference between
    Frappe versions (`if_owner` was added to `add_permission` later)."""
    try:
        frappe.permissions.add_permission(doctype, role, 0, ptype, if_owner=if_owner)
    except TypeError:
        frappe.get_doc(
            {
                "doctype": "Custom DocPerm",
                "parent": doctype,
                "parenttype": "DocType",
                "parentfield": "permissions",
                "role": role,
                "permlevel": 0,
                "if_owner": if_owner,
                ptype: 1,
            }
        ).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Parsing a SOP into a draft template
# ---------------------------------------------------------------------------


@frappe.whitelist()
def parse_sop_form(sop):
    """Read the SOP's own document and return (and store) a draft form.

    Regenerating is explicit, and replaces the *draft* only. A published form
    is never touched: revising a controlled document is a new version, not an
    edit to the record of what was already run.
    """
    _require_admin()
    _refuse_frozen(sop, "extraction form")
    name = _draft_from_document(sop, replace=True)
    return _sop_payload(frappe.get_doc("SOP Form Template", name))


def on_sop_before_save(doc, method=None):
    """A SOP obeys document control, not just its author.

    Three rules, and they hold however the record is saved -- Console, Desk,
    REST or a script:

    * a SOP is created as a Draft, always: activation is a signed approval, not
      a value typed into a form;
    * its status changes only through that signature, so a document cannot be
      put in force by editing a field;
    * a frozen SOP -- one a completed run cites -- cannot change at all. The
      document a run followed is evidence; a revision is a new record.
    """
    previous = doc.get_doc_before_save()

    if (
        previous
        and previous.get("frozen_on")
        and not frappe.flags.get("sop_control_write")
    ):
        changed = [
            df.label or df.fieldname
            for df in doc.meta.fields
            if df.fieldtype not in ("Section Break", "Column Break", "Tab Break")
            and df.fieldname not in FROZEN_ALLOWED
            and doc.has_value_changed(df.fieldname)
        ]
        if changed:
            frappe.throw(
                f"SOP {doc.name} was frozen on {previous.frozen_on} because a "
                f"completed extraction run cites it. Its "
                f"{', '.join(changed[:5])} cannot be changed. Issue a new version "
                f"from it instead."
            )

    if doc.is_new():
        # Even an admin's new SOP starts as a Draft: approval is the signature
        # that puts a version in force.
        doc.status = "Draft"
        return

    if doc.has_value_changed("status") and not frappe.flags.get("sop_signature"):
        frappe.throw(
            "A SOP's status changes through a signed approval or supersession, "
            "not by editing it. Use Approve or Mark superseded on the SOP's "
            "page, both of which ask for your password and a reason."
        )


def on_sop_created(doc, method=None):
    """Draft the form as soon as a SOP with a document exists.

    Done here rather than on a button so a client's upload arrives at the
    admin's review queue already parsed. A parse that fails leaves the SOP
    intact and says so in the log -- a document in an unfamiliar house style is
    the cue to build the form by hand, not a reason to refuse the SOP.
    """
    if doc.status != "Draft":
        return
    _draft_quietly(doc)


def on_sop_updated(doc, method=None):
    """Draft the form when a SOP gains a document after it was created.

    A SOP is often created first and its batch form attached afterwards -- on
    the SOP's own page, in Desk, or by a client following up on an email. That
    is the same act as uploading it on the first save, so it gets the same
    treatment rather than waiting for someone to find a button.

    What is deliberately *not* touched: an existing draft (it may carry an
    admin's corrections), a published form (it is the record of what runs
    followed), and a frozen or superseded SOP (its form is evidence). Reading
    a changed document over a published form stays an explicit admin action.
    """
    if doc.status == "Superseded" or doc.get("frozen_on"):
        return
    if frappe.db.exists("SOP Form Template", {"sop": doc.name}):
        return
    _draft_quietly(doc)


def _draft_quietly(doc):
    """Read the SOP's document into a draft, and leave the SOP saved if it fails.

    A document in an unfamiliar house style is the cue to build the form by
    hand; it is not a reason to refuse the SOP or to break the save that
    carried it. The failure is logged with the document's URL so the admin can
    see why no form appeared.
    """
    if not doc.document:
        return
    try:
        _draft_from_document(doc.name, replace=False)
    except Exception:
        frappe.log_error(
            title="Symbiotope Traceability",
            message=f"Could not parse the form out of SOP {doc.name} "
            f"({doc.document}). The SOP was saved; its form needs to be built "
            f"or corrected by hand.\n\n{frappe.get_traceback()}",
        )


def _draft_from_document(sop, replace):
    sop_doc = frappe.get_doc("SOP", sop)
    _refuse_frozen(sop_doc.name, "extraction form")
    if sop_doc.status == "Superseded":
        frappe.throw(
            f"SOP {sop_doc.name} is superseded, so its form is history. "
            f"Read the form on the version now in force instead."
        )
    if not sop_doc.document:
        frappe.throw(f"SOP {sop_doc.name} has no document attached, so there is nothing to read.")

    path, digest = _document_file(sop_doc)
    form = parse_docx(path)
    rows = _rows_from_form(form)
    if not rows:
        frappe.throw(
            "No fields could be read out of this document. Its layout is "
            "probably not the numbered-section house style the parser knows; "
            "the form will have to be built by hand."
        )

    existing = _template_for(sop_doc.name, ("Draft", "Published"))
    if existing and existing.status == "Published":
        if not replace:
            return existing.name
        # A published form is the record of what runs already followed. Leave
        # it alone and start a new draft; publishing the draft supersedes it.
        existing = None

    if existing and replace:
        frappe.delete_doc("SOP Form Template", existing.name, ignore_permissions=True, force=True)

    template = frappe.get_doc(
        {
            "doctype": "SOP Form Template",
            "sop": sop_doc.name,
            "sop_version": sop_doc.version,
            "title": form.get("title") or sop_doc.title,
            "status": "Draft",
            "source_document": sop_doc.document,
            "source_hash": digest,
            "field_count": len(rows),
            "fields": rows,
        }
    )
    template.insert(ignore_permissions=True)
    return template.name


def _document_file(sop_doc):
    """The attachment's path on disk, plus a hash of the bytes.

    The hash is what makes regeneration honest: if the attached file changed,
    the draft is out of date and says so, rather than pretending the published
    form still describes the document on file.
    """
    import hashlib

    names = frappe.get_all("File", filters={"file_url": sop_doc.document}, pluck="name", limit=1)
    if not names:
        frappe.throw(f"The attached document {sop_doc.document} is not in the file store.")
    file_doc = frappe.get_doc("File", names[0])
    path = file_doc.get_full_path()
    with open(path, "rb") as handle:
        digest = hashlib.sha1(handle.read()).hexdigest()
    return path, digest


def _rows_from_form(form):
    """Flatten the parsed document into template rows, assigning keys once.

    Keys are derived from section/block/label so they are stable across a
    reparse of an unchanged document, and an answer already recorded against a
    field survives the form being regenerated. They are also opaque, so
    rewording a label later does not silently move a value to another question.
    """
    seen = {}
    rows = []
    sequence = 0

    def add(section, block_ref, block, fields):
        nonlocal sequence
        for field in fields:
            sequence += 1
            field_type = field.get("field_type") or "text"
            rows.append(
                {
                    "sequence": sequence,
                    "section": section,
                    "block_ref": block_ref,
                    "block": block.get("label"),
                    "block_type": block.get("type") or "fields",
                    "field_key": field_key(section, block.get("label"), field["label"], seen),
                    "label": field["label"],
                    "field_type": field_type,
                    "options": "\n".join(field.get("options") or []),
                    "unit": field.get("unit"),
                    "specification": field.get("spec") or None,
                    "note": field.get("note"),
                    "required": _default_required(field_type, field.get("role")),
                    "role": field.get("role") or "input",
                }
            )

    for block_ref, block in enumerate(form["header"]["blocks"]):
        add("Kop", block_ref, block, block["fields"])
    for section in form["sections"]:
        for block_ref, block in enumerate(section["blocks"]):
            add(section["title"], block_ref, block, block["fields"])
    return rows


def _default_required(field_type, role):
    """Typed answers are required; ticks are not.

    A checklist row that never applied to this run must not read as a missing
    value, and the parser cannot tell "this applies" from "this was checked".
    The admin flips the ones the SOP actually demands.
    """
    if role and role != "input":
        return 0
    return 1 if field_type in TYPED else 0


# ---------------------------------------------------------------------------
# Reviewing and publishing
# ---------------------------------------------------------------------------


@frappe.whitelist()
def save_sop_form(template, fields):
    """Store an admin's corrections to a draft.

    Fields arrive as the whole list, in order: the Console edits the form as a
    document, not as a series of moves, so replacing the table wholesale is the
    honest representation of what changed.
    """
    _require_admin()
    doc = frappe.get_doc("SOP Form Template", template)
    if doc.status != "Draft":
        frappe.throw(
            f"Form {doc.name} is {doc.status}. Only a draft can be edited; "
            f"a published form is the record of what runs were filled in against."
        )
    _refuse_frozen(doc.sop, "extraction form")

    rows = _clean_rows(_loads(fields))
    if not rows:
        frappe.throw("A form needs at least one field.")

    doc.set("fields", [])
    for row in rows:
        doc.append("fields", row)
    doc.field_count = len(rows)
    doc.save(ignore_permissions=True)
    return _sop_payload(doc)


@frappe.whitelist()
def publish_sop_form(template):
    """Freeze a reviewed draft so extraction runs can use it.

    Publishing supersedes the previous published form of the same SOP: two
    published forms for one procedure would make "the form this run followed"
    ambiguous, which is the one thing a batch record cannot be.
    """
    _require_admin()
    doc = frappe.get_doc("SOP Form Template", template)
    if doc.status == "Published":
        return _sop_payload(doc)

    _refuse_frozen(doc.sop, "extraction form")
    if frappe.db.get_value("SOP", doc.sop, "status") == "Superseded":
        frappe.throw(
            f"SOP {doc.sop} is superseded, so no further form can be published "
            f"for it. Issue a new version instead."
        )

    problems = _publish_problems(doc)
    if problems:
        frappe.throw("This form cannot be published yet:<br><br>" + "<br>".join(problems))

    for other in frappe.get_all(
        "SOP Form Template",
        filters={"sop": doc.sop, "status": "Published", "name": ("!=", doc.name)},
        pluck="name",
    ):
        frappe.db.set_value("SOP Form Template", other, "status", "Superseded")

    doc.status = "Published"
    doc.reviewed_by = frappe.session.user
    doc.published_on = now_datetime()
    doc.field_count = len(doc.fields)
    doc.save(ignore_permissions=True)
    return _sop_payload(doc)


def _publish_problems(doc):
    problems = []
    fields = doc.fields or []

    if not fields:
        return ["The form has no fields."]

    keys = [row.field_key for row in fields]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        problems.append(f"Duplicate field keys: {', '.join(duplicates[:10])}.")

    blank_labels = [row.field_key for row in fields if not cstr(row.label).strip()]
    if blank_labels:
        problems.append(f"{len(blank_labels)} field(s) have no label.")

    missing_options = [
        row.label for row in fields
        if row.field_type in ("select", "checkbox_group") and not (row.options or "").strip()
    ]
    if missing_options:
        problems.append(
            "These choice fields have no options: " + ", ".join(missing_options[:8]) + "."
        )

    sections = {row.section for row in fields if row.section}
    if not sections:
        problems.append("No section headings were found, so the form would have no structure.")

    return problems


def _clean_rows(rows):
    """Normalise what the Console sends back into template rows."""
    cleaned = []
    used = set()
    for row in rows:
        label = cstr(row.get("label")).strip()
        if not label:
            continue
        field_type = row.get("field_type") if row.get("field_type") in FIELD_TYPES else "text"
        section = cstr(row.get("section")).strip() or "Kop"
        block_label = cstr(row.get("block")).strip() or None
        key = cstr(row.get("field_key")).strip() or field_key(
            section, block_label, label, {}
        )
        # A key that repeats would make two questions share one answer, so the
        # second one is moved rather than silently merged.
        base, suffix = key, 1
        while key in used:
            suffix += 1
            key = f"{base}_{suffix}"
        used.add(key)

        options = row.get("options")
        if isinstance(options, (list, tuple)):
            options = "\n".join(cstr(option).strip() for option in options if cstr(option).strip())
        else:
            options = "\n".join(
                line.strip() for line in cstr(options).splitlines() if line.strip()
            )

        cleaned.append(
            {
                "sequence": int(row.get("sequence") or len(cleaned) + 1),
                "section": section,
                "block_ref": int(row.get("block_ref") or 0),
                "block": block_label,
                "block_type": row.get("block_type") if row.get("block_type") in BLOCK_TYPES else "fields",
                "field_key": key,
                "label": label,
                "field_type": field_type,
                "options": options,
                "unit": cstr(row.get("unit")).strip() or None,
                "specification": cstr(row.get("specification")).strip() or None,
                "note": cstr(row.get("note")).strip() or None,
                "required": 1 if int(row.get("required") or 0) else 0,
                "role": row.get("role") if row.get("role") in ROLES else "input",
            }
        )
    return cleaned


# ---------------------------------------------------------------------------
# Reading forms (Console)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_sop_form(sop):
    """The form for a SOP, published if there is one, else the draft.

    The Console shows both: a draft is what an admin reviews, a published form
    is what an operator will be asked. The `status` in the payload is what the
    button is chosen by, not a guess by the client about permissions.
    """
    # A client may read the form of a SOP they uploaded; Frappe's own perm row
    # (if_owner) is what decides which SOPs those are.
    if not CONSOLE_ROLES.intersection(frappe.get_roles()):
        if not frappe.has_permission("SOP", "read", sop):
            frappe.throw(
                "Not permitted: this account holds no Symbiotope Console role.",
                frappe.PermissionError,
            )
    template = _template_for(sop, ("Published", "Draft"))
    if not template:
        return {
            "sop": sop,
            "sop_version": None,
            "template": None,
            "title": None,
            "status": None,
            "field_count": 0,
            "source_hash": None,
            "reviewed_by": None,
            "published_on": None,
            "sections": [],
            "fields": [],
            "published": None,
            "sop_status": frappe.db.get_value("SOP", sop, "status"),
            "frozen_on": _sop_frozen(sop),
        }

    return _sop_payload(template)


@frappe.whitelist()
def list_published_forms():
    """Every SOP that can start a run now, for the create-batch picker.

    A SOP without a published form is deliberately absent: choosing it would
    produce a run with no record to fill in.
    """
    _require_console_role()
    templates = frappe.get_all(
        "SOP Form Template",
        filters={"status": "Published"},
        fields=["name", "sop", "sop_version", "title", "field_count"],
        order_by="sop asc",
    )
    for template in templates:
        template["sop_title"] = frappe.db.get_value("SOP", template["sop"], "title")
        template["sop_status"] = frappe.db.get_value("SOP", template["sop"], "status")
    return [t for t in templates if t["sop_status"] == "Active"]


@frappe.whitelist()
def get_extraction_form(batch):
    """The batch record of a run: structure from the snapshot, values beside it."""
    _require_console_role()
    doc = frappe.get_doc("Extraction Batch", batch)
    return {
        "batch": doc.name,
        "sop": doc.sop,
        "sop_version": doc.sop_version_snapshot,
        "template": doc.form_template,
        "status": doc.status,
        "editable": doc.status != "Complete",
        "sections": _sections(doc.form_values or []),
        # Labels read well in a message; keys are what the renderer marks, so a
        # field whose label repeats elsewhere in the form is marked correctly.
        "missing": _missing(doc.form_values or []),
        "missing_keys": _missing(doc.form_values or [], by_key=True),
        "override_reason": doc.get("form_override_reason"),
    }


@frappe.whitelist()
def save_extraction_form(batch, values):
    """Record answers against a run.

    Takes a `{field_key: value}` map rather than the whole document, so a form
    that has been autosaving does not race itself: two people filling different
    sections both succeed, and neither overwrites the other's answers.
    """
    _require_console_role()
    doc = frappe.get_doc("Extraction Batch", batch)
    doc.check_permission("write")
    if doc.status == "Complete":
        frappe.throw(
            f"Extraction {doc.name} is complete. Its batch record is closed and "
            f"cannot be changed; a correction is a deviation recorded against "
            f"the run, not an edit to it."
        )

    answers = _loads(values) or {}
    if not isinstance(answers, dict):
        frappe.throw("values must be a map of field key to value.")

    by_key = {row.field_key: row for row in doc.form_values}
    for key, value in answers.items():
        row = by_key.get(key)
        if row is None:
            continue
        row.value = _value_for(row, value)

    doc.save()
    return {
        "batch": doc.name,
        "missing": _missing(doc.form_values),
        "missing_keys": _missing(doc.form_values, by_key=True),
        "sections": _sections(doc.form_values),
    }


def _value_for(row, value):
    """Normalise an answer to what the field's type stores."""
    if value is None:
        return None
    if row.field_type == "checkbox":
        return "1" if cstr(value) in ("1", "true", "True", "on", "Yes") else "0"
    if row.field_type == "checkbox_group":
        if isinstance(value, (list, tuple)):
            return "\n".join(cstr(item).strip() for item in value if cstr(item).strip())
        return cstr(value).strip()
    return cstr(value).strip()


# ---------------------------------------------------------------------------
# The Extraction Batch side
# ---------------------------------------------------------------------------


def sync_form_values(doc):
    """Copy the SOP's published form onto a run the moment it is attached.

    The snapshot happens once. Re-saving a run never re-reads the form, so a
    published form superseded (or even edited out of band) later cannot change
    what an in-flight run is being asked for.

    A SOP with no published form is refused on a *new* run, because the whole
    point of choosing a SOP first is that the form follows it. An existing run
    is left alone: batches that predate forms must still be savable.
    """
    if not doc.sop:
        if doc.form_template:
            doc.form_template = None
            doc.set("form_values", [])
        return

    if doc.form_values:
        return

    template = _template_for(doc.sop, ("Published",))
    if not template:
        if doc.is_new():
            frappe.throw(
                f"SOP {doc.sop} has no published extraction form, so this run "
                f"would have no batch record to fill in. Ask an admin to review "
                f"and publish its form first."
            )
        return

    doc.form_template = template.name
    for row in template.fields:
        doc.append(
            "form_values",
            {
                "sequence": row.sequence,
                "section": row.section,
                "block_ref": row.block_ref,
                "block": row.block,
                "block_type": row.block_type,
                "field_key": row.field_key,
                "label": row.label,
                "field_type": row.field_type,
                "options": row.options,
                "unit": row.unit,
                "specification": row.specification,
                "note": row.note,
                "required": row.required,
                "role": row.role,
                "value": None,
            },
        )


def validate_form_completion(doc):
    """Refuse to complete a run whose SOP says more was to be recorded.

    An admin can override, and the override is part of the record: who, when
    and why. That is the difference between "the form blocked us" and "we knew
    and decided", which is exactly what an auditor asks.
    """
    missing = _missing(doc.form_values or [])
    if not missing:
        return

    reason = cstr(doc.get("form_override_reason")).strip()
    if reason:
        if not ADMIN_ROLES.intersection(frappe.get_roles()):
            frappe.throw(
                f"{len(missing)} required field(s) are empty and only an admin "
                f"may complete a run with them missing."
            )
        doc.form_override_by = frappe.session.user
        doc.form_override_on = now_datetime()
        return

    shown = "; ".join(missing[:8])
    more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
    frappe.throw(
        f"{len(missing)} required field(s) from the SOP's form are still empty: "
        f"{shown}{more}. Fill them in, or have an admin complete the run with a "
        f"recorded override reason."
    )


def _missing(rows, by_key=False):
    """The required fields still without an answer, in form order.

    Labels by default, because that is what an operator is shown; keys on
    request, because labels repeat within a form and only a key identifies the
    one input to mark.
    """
    missing = []
    for row in rows:
        if not row.required or row.role != "input":
            continue
        if cstr(row.value).strip():
            continue
        missing.append(row.field_key if by_key else row.label)
    return missing


# ---------------------------------------------------------------------------
# Shared shape
# ---------------------------------------------------------------------------


def _template_for(sop, statuses):
    """The one form to show for a SOP, preferring a draft over a published one.

    When both exist -- a revision is being prepared against a form runs are
    already using -- the draft is the thing to look at, because it is what the
    admin still has to act on. Ordering by status does that: Draft sorts before
    Published. Nothing else reads two statuses at once (a run only ever wants
    the published one), so this cannot pick the wrong form for a run.
    """
    names = frappe.get_all(
        "SOP Form Template",
        filters={"sop": sop, "status": ("in", statuses)},
        order_by="status asc, published_on desc, creation desc",
        limit=1,
        pluck="name",
    )
    return frappe.get_doc("SOP Form Template", names[0]) if names else None


def _sections(rows):
    """Rows regrouped into the document's sections and blocks.

    Section and block come from the snapshot, not from today's template, so a
    closed batch record still reads exactly as it was filled in.
    """
    sections = []
    for row in rows:
        if not sections or sections[-1]["title"] != row.section:
            sections.append({"title": row.section, "blocks": []})
        blocks = sections[-1]["blocks"]
        if (
            not blocks
            or blocks[-1]["ref"] != row.block_ref
            or blocks[-1]["type"] != row.block_type
        ):
            blocks.append(
                {"ref": row.block_ref, "label": row.block, "type": row.block_type, "fields": []}
            )
        blocks[-1]["fields"].append(_field_payload(row))
    return sections


def _field_payload(row):
    # A template row has no answer; a snapshot row does. The shipped
    # document-control values live in `note` until a run is created, so the
    # published preview still prints the SOP code and version.
    value = getattr(row, "value", None)
    if value is None and row.role == "data":
        value = row.note
    return {
        "key": row.field_key,
        "label": row.label,
        "type": row.field_type,
        "options": [line for line in cstr(row.options).splitlines() if line.strip()],
        "unit": row.unit,
        "specification": row.specification,
        "note": row.note,
        "required": bool(row.required),
        "role": row.role,
        "value": value,
    }


def _sop_payload(template):
    """A template plus, when it is a draft, the published form still in use.

    A draft revision can coexist with the form runs are actually being filled
    in against. Saying so on the page is the difference between "this SOP has no
    form" and "its form is being revised".
    """
    payload = _template_payload(template)
    payload["sop_status"] = frappe.db.get_value("SOP", template.sop, "status")
    payload["frozen_on"] = _sop_frozen(template.sop)
    live = (
        _template_for(template.sop, ("Published",))
        if template.status == "Draft"
        else None
    )
    payload["published"] = (
        {
            "template": live.name,
            "published_on": live.published_on,
            "field_count": len(live.fields or []),
        }
        if live
        else None
    )
    return payload


def _template_payload(doc):
    return {
        "sop": doc.sop,
        "sop_version": doc.sop_version,
        "template": doc.name,
        "title": doc.title,
        "status": doc.status,
        "field_count": len(doc.fields or []),
        "source_hash": doc.source_hash,
        "reviewed_by": doc.reviewed_by,
        "published_on": doc.published_on,
        "sections": _sections(doc.fields or []),
        "fields": [
            {
                "sequence": row.sequence,
                "section": row.section,
                "block_ref": row.block_ref,
                "block": row.block,
                "block_type": row.block_type,
                "field_key": row.field_key,
                "label": row.label,
                "field_type": row.field_type,
                "options": row.options,
                "unit": row.unit,
                "specification": row.specification,
                "note": row.note,
                "required": bool(row.required),
                "role": row.role,
            }
            for row in (doc.fields or [])
        ],
    }


def _require_admin():
    if not ADMIN_ROLES.intersection(frappe.get_roles()):
        frappe.throw(
            "Not permitted: only an admin may review or publish an extraction form.",
            frappe.PermissionError,
        )


def _require_console_role():
    if not CONSOLE_ROLES.intersection(frappe.get_roles()):
        frappe.throw(
            "Not permitted: this account holds no Symbiotope Console role.",
            frappe.PermissionError,
        )


def _loads(value):
    """Accept JSON as a string (what a POST body carries) or already-parsed."""
    if isinstance(value, (dict, list)):
        return value
    text = cstr(value).strip()
    if not text:
        return None
    return json.loads(text)
