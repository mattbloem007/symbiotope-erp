"""Turn a client's SOP batch form into structured fields.

A batch record is not a bag of inputs. The SOP's own document decides which
values an operator records, in which order, with which specification beside
them, and which tick-boxes are mutually exclusive. So the extraction form is
generated from the SOP rather than written in code: choose a different SOP and
the run asks for different things.

This module reads the .docx and proposes sections, blocks and fields. What it
produces is a *draft*. It cannot know that the second column of a table is the
specification while the third is what the operator writes, nor that
"Goedgekeurde grondstof: Nee" must send someone to a deviation report. An admin
confirms the draft before anyone can run to it (see `publish_sop_form`),
because a form that is silently wrong is worse than no form at all: it produces
a complete-looking batch record that is not what the SOP says.

The reading is shape-based, not word-based. Client SOPs share a house style --
numbered ALL-CAPS section headings, tables whose first row names the columns,
tick sequences, and `____` blanks -- so the rules below describe that style
rather than the wording of one document. A document in another style yields a
poor draft, which is the admin's cue to build the form by hand.
"""

import hashlib
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# The empty box, plus the glyphs Word leaves behind when an author typed a
# different character. A *ticked* box in a blank template is the author's
# mistake; it is read as an option, not as an answer already given.
BOXES = "\u2610\u2611\u2612\u25a1"

BLANK = re.compile(r"_{3,}|\.{5,}")
SECTION = re.compile(r"^\s*(\d{1,2})[.)]\s+([^\s].{2,})$")
NUMBERED = re.compile(r"^\s*\d{1,2}\s*[.)]\s+")

SPEC_HEADS = {"specificatie", "specificatie / limiet", "eis", "eisen", "norm", "limiet"}
ACTUAL_HEADS = {
    "werkelijk", "werkelijk resultaat", "waarneming", "invullen", "in te vullen",
    "resultaat", "gemeten", "meting", "waarde", "gegeven", "gegevens",
}
CHECK_HEADS = {"gereinigd", "gecontroleerd", "gedaan", "uitgevoerd", "aanwezig"}
UNIT_HEADS = {"°c", "c", "mbar", "rpm", "l", "ml", "min", "uur", "%"}
LABEL_HEADS = {"gegeven", "gegevens", "parameter", "item", "onderdeel"}
# Box sets that ask for every tick that applies. Everything else is a choice:
# a form may record one productiestatus, not three.
MULTI_HINTS = ("controle", "checklist", "opslag")

YES_NO = {"ja", "nee"}
UNITS = {"%": "%", "°c": "°C", "mbar": "mbar", "rpm": "rpm", "l": "L", "ml": "ml",
         "min": "min", "uur": "uur"}


def parse_docx(path):
    """`{"title", "header", "sections"}` for the document at `path`."""
    return _parse(_blocks(path))


def iter_fields(form):
    """Every field in the form with its section and block, in document order."""
    for block in form["header"]["blocks"]:
        for field in block["fields"]:
            yield {"section": "Kop", "block": None, "field": field}
    for section in form["sections"]:
        for block in section["blocks"]:
            for field in block["fields"]:
                yield {"section": section["title"], "block": block.get("label"), "field": field}


def field_key(section, block, label, seen):
    """A stable, opaque key, so answers outlive a reworded label."""
    seed = "|".join([section or "", block or "", label or ""])
    slug = re.sub(r"[^a-z0-9]+", "_", _norm(seed)).strip("_")
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6]
    key = f"{slug[:70]}_{digest}" if slug else digest
    count = seen.get(key, 0)
    seen[key] = count + 1
    return key if not count else f"{key}_{count}"


# ---------------------------------------------------------------------------
# Reading the .docx
# ---------------------------------------------------------------------------


def _blocks(path):
    """Paragraphs and tables in document order.

    Order is the point: a caption names the table below it, and a heading opens
    the section that both belong to.
    """
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    blocks = []
    for child in root.find(W + "body"):
        if child.tag == W + "p":
            text = _paragraph_text(child)
            if text:
                blocks.append({"kind": "p", "text": text})
        elif child.tag == W + "tbl":
            rows = []
            for tr in child.findall(W + "tr"):
                cells = [_cell_text(tc) for tc in tr.findall(W + "tc")]
                if any(c.strip() for c in cells):
                    rows.append(cells)
            if rows:
                blocks.append({"kind": "tbl", "rows": rows})
    return blocks


def _paragraph_text(p):
    """The paragraph's text, blanks normalised.

    A blank is one thing however it is drawn: `____ – ____` is a range and
    `__ / __ / ____` a date, not three separate questions. Collapsing them here
    keeps every rule below from having to know about it.
    """
    text = "".join(t.text or "" for t in p.iter(W + "t"))
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"_{3,}\s*[–—]\s*_{3,}", "______", text)
    text = re.sub(r"_{3,}\s*/\s*_{3,}(?:\s*/\s*_{3,})?", "______", text)
    # Word often keeps a caption and its block's first field in one paragraph
    # ("...(menstruum)Geplande verhouding"). Lowercase running straight into a
    # capitalised word is that seam.
    text = re.sub(r"(?<=[a-z)\]])(?=[A-Z][a-z])", " ", text)
    return text.strip()


def _cell_text(tc):
    """One cell, its paragraphs joined.

    A cell holds one value even when the author wrapped it; a newline would
    make it unmatchable to the word printed beside it.
    """
    parts = [_paragraph_text(p) for p in tc.findall(W + "p")]
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# The document as a form
# ---------------------------------------------------------------------------


def _parse(blocks):
    title = ""
    header = {"title": "", "blocks": []}
    sections = []
    section = None
    caption = None
    pending = None  # a question whose answer line follows
    first_table = True

    def current():
        return section or header

    def flush_pending():
        """A `Beschrijving afwijking:` with nothing after it is still a field.

        In the paper form the answer goes on the blank lines underneath; here
        it is a text area, so the question cannot be dropped for lack of a
        `____`.
        """
        nonlocal pending
        if pending:
            current()["blocks"].append(
                {"type": "fields", "label": None,
                 "fields": [_field("long_text", pending)]}
            )
            pending = None

    for index, block in enumerate(blocks):
        if block["kind"] == "p":
            text = block["text"]

            if not title and index == 0:
                title = header["title"] = text
                continue

            heading = _section_heading(text)
            if heading:
                flush_pending()
                number, name = heading
                section = {"number": number, "title": _title_case(name), "blocks": []}
                sections.append(section)
                caption = None
                continue

            if _is_note(text):
                continue

            yes_no = _yes_no_question(text)
            if yes_no:
                flush_pending()
                current()["blocks"].append(
                    {"type": "fields", "label": None,
                     "fields": [_field("select", yes_no, options=["Ja", "Nee"])]}
                )
                caption = None
                continue

            label, fields, bare = _fields_from_paragraph(text, caption)
            if fields:
                last = _last_field(current())
                if bare and pending:
                    fields[0]["label"] = pending
                    pending = None
                    last = None
                elif bare and caption:
                    # A caption directly above a tick line names that choice:
                    # "Controle" then the checklist.
                    fields[0]["label"] = _group_label(caption)
                    caption = None
                    last = None
                if bare and fields[0]["field_type"] in ("select", "checkbox_group") and last:
                    # `☐ Ja` on its own line belongs to the question above it.
                    last["options"] = _dedupe(last["options"] + fields[0]["options"])
                    last["field_type"] = _group_type(last["label"], last["options"], caption)
                    caption = None
                    continue
                flush_pending()
                current()["blocks"].append({"type": "fields", "label": label, "fields": fields})
                caption = None
                continue

            if _is_question(text):
                following = blocks[index + 1] if index + 1 < len(blocks) else None
                if following and following["kind"] == "tbl":
                    # `Sensorische meting eindproduct:` introduces the table
                    # below it; the answer is not a text area.
                    caption = _clean(text)
                    continue
                cleaned = _clean(text)
                if cleaned:
                    flush_pending()
                    pending = cleaned
                continue

            if _looks_like_caption(text):
                caption = _clean(text)

        else:
            flush_pending()
            if first_table and _is_doc_control(block["rows"]):
                header["blocks"].append(_doc_control(block["rows"]))
            else:
                table = _table(block["rows"], caption)
                if table:
                    current()["blocks"].append(table)
            first_table = False
            caption = None

    flush_pending()
    header["blocks"] = [b for b in header["blocks"] if b["fields"]]
    return {"title": title, "header": header, "sections": [s for s in sections if s["blocks"]]}


def _last_field(target):
    """The final field of the last block, if that block is a field block."""
    if not target["blocks"]:
        return None
    block = target["blocks"][-1]
    if block["type"] != "fields" or not block["fields"]:
        return None
    field = block["fields"][-1]
    return field if field["field_type"] in ("select", "checkbox_group") else None


def _group_label(caption):
    """The question inside a caption.

    `Grondstof inspectie: Aanwezigheid van vreemde stoffen (...)` is a heading
    and its question in one line; only the part after the colon is asked.
    """
    if ":" in caption:
        after = caption.split(":", 1)[1].strip()
        if after:
            return _clean(after)
    return _clean(caption)


def _section_heading(text):
    """`1. GRONDSTOF` -> (1, "GRONDSTOF"). Only a heading if it is shouted."""
    match = SECTION.match(text)
    if not match:
        return None
    name = match.group(2).strip()
    letters = [c for c in name if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.8:
        return None
    return int(match.group(1)), name


def _is_note(text):
    """Footnotes and standing instructions that belong to no field."""
    return text.startswith("*") or text.lower().startswith(
        ("let op", "n.b.", "nodig:", "benodigd", "bij tussentijds")
    )


def _yes_no_question(text):
    """`Vacuum: ja/nee` -> "Vacuum". A question with its answers named inline."""
    match = re.match(r"^(.{2,80}?)\s*:\s*(ja|nee)\s*/\s*(ja|nee)\s*$", text, re.I)
    return _clean(match.group(1)) if match else None


def _is_question(text):
    """A labelled question with no blank: the answer is the space beneath it."""
    if _has_boxes(text) or BLANK.search(text):
        return False
    if not text.endswith(":") or len(text) > 90:
        return False
    # A shouted line is the heading of the block below, not a question.
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return False
    return len(_clean(text)) > 2


def _looks_like_caption(text):
    """A bare line introducing the block below it (`Voorbewerking`)."""
    if len(text) > 200 or BLANK.search(text) or _has_boxes(text):
        return False
    if NUMBERED.match(text):
        return False
    return not text.lower().endswith(("ja/nee", "ja / nee"))


def _title_case(name):
    """`KLAARZETTEN & CONTROLE` -> `Klaarzetten & controle`.

    Sentence case, not title case: the names are Dutch phrases, and
    capitalising every word makes them read like a different language.
    """
    small = {"&", "en", "van", "de", "het", "voor", "naar", "bij"}
    out = []
    for i, word in enumerate(name.lower().split()):
        out.append(word if i and word in small else word[:1].upper() + word[1:])
    return " ".join(out)


# ---------------------------------------------------------------------------
# Paragraphs that carry fields
# ---------------------------------------------------------------------------


def _fields_from_paragraph(text, caption):
    """(block label, fields, all-options-with-no-question) for one paragraph.

    One line can ask for several things: `Grinden / malen: ☐ Nee ☐ Ja → type
    apparatuur: ____` is a choice *and* the text that choice unlocks.
    """
    if not _has_boxes(text) and not BLANK.search(text):
        return None, [], False

    bare = _has_boxes(text) and not _clean(re.split(f"[{BOXES}]", text)[0])

    if _has_boxes(text) and not BLANK.search(text):
        return caption, _choice_fields(text, caption), bare

    fields = []
    carry = ""
    for segment, tail in _split_on_blanks(text):
        label = _clean(carry or segment)
        carry = ""
        if _has_boxes(label):
            fields.extend(_choice_fields(label, caption, tail))
            continue
        if not label:
            label = caption or ""
            if not label:
                continue
        field = _field(_infer_type(label), label)
        unit, carry = _tail_parts(tail)
        if unit:
            field["unit"] = unit
            if field["field_type"] == "text":
                field["field_type"] = "percent" if unit == "%" else "number"
        fields.append(field)
    return caption, fields, bare


UNIT_TOKEN = re.compile(r"^(%|°C|mbar|rpm|L|ml|min|uur)\s*(.+?):$", re.I)


def _tail_parts(tail):
    """(unit, the label of the field that follows) after a blank.

    `Dropgewicht: ____ %Specificatie: ____` -- the percent belongs to the field
    before and "Specificatie" is the field after; the blank only carried the
    unit, so it must not become a field of its own called "%Specificatie".
    """
    tail = (tail or "").strip()
    if not tail:
        return None, ""
    match = UNIT_TOKEN.match(tail)
    if match:
        token = match.group(1)
        return UNITS.get(token.lower(), token), match.group(2)
    if tail.lower() in UNITS:
        return UNITS[tail.lower()], ""
    return None, ""


def _split_on_blanks(text):
    """[(text before each blank, text up to the next blank)] in reading order.

    The tail stops at the following blank: `____ %Specificatie: ____` has one
    unit after the first blank and a label after it, not the rest of the line.
    """
    matches = list(BLANK.finditer(text))
    out = []
    for i, match in enumerate(matches):
        start = matches[i - 1].end() if i else 0
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((text[start:match.start()], text[match.end():end]))
    return out


def _choice_fields(text, caption, tail=""):
    """A `☐` sequence as a choice or a multi-tick group, plus what it opens."""
    options = _options(text)
    labels = [o["label"] for o in options if o["label"]]
    if not labels:
        return []

    prefix = _clean(re.split(f"[{BOXES}]", text)[0])
    label = prefix or (caption or "Keuze")
    fields = [_field(_group_type(label, labels, caption), label, options=labels)]

    for option in options:
        if option["then"] and not _is_reference(option["then"]):
            fields.append(_field(_infer_type(option["then"]), option["then"]))
    if tail and tail.lower() in UNITS:
        fields[-1]["unit"] = UNITS[tail.lower()]
    return fields


def _group_type(label, options, caption=""):
    """One choice, or every tick that applies.

    A form records one productiestatus and one Vorm; it can record several
    pieces of equipment cleaned. Only the second is a tick list, and the
    caption is what says so.
    """
    hint = f"{label} {caption or ''}".lower()
    if any(word in hint for word in MULTI_HINTS) and len(options) > 1:
        return "checkbox_group"
    return "select"


def _options(text):
    """[{"label", "then"}] for each box in `text`.

    `then` is what the option opens: the text after `→`, or the box after
    `Anders:` where the operator names the other thing.
    """
    if not _has_boxes(text):
        return []
    out = []
    for part in re.split(f"[{BOXES}]", text)[1:]:
        part = part.strip()
        label, then = part, ""
        if "→" in part:
            label, then = part.split("→", 1)
        elif re.match(r"(?i)and(ers|ere)\b", part):
            label = re.match(r"(?i)and(ers|ere)\b", part).group(0)
            # `Anders: ____` -- the blank is where the name goes, so it needs a
            # field of its own or the answer has nowhere to be written.
            then = part[len(label):].strip(" :;,-") or "Anders, namelijk"
        label = _clean(label)
        then = _clean(then)
        if label:
            out.append({"label": label, "then": then})
    return out


def _is_reference(text):
    """`→ zie hoofdstuk 8` points at a section; it is not an input."""
    return text.lower().startswith(("zie ", "zie hoofdstuk", "hoofdstuk"))


def _has_boxes(text):
    return any(box in text for box in BOXES)


def _strip_boxes(text):
    for box in BOXES:
        text = text.replace(box, " ")
    return _clean(text)


def _is_hint(cell):
    """An example printed beside the field rather than an answer to it."""
    return cell.startswith("(") or " bv " in f" {cell} "


def _type_for(heading, label=""):
    """The type a measurement column asks for, from its heading."""
    low = heading.lower()
    if "drooggewicht" in low or "%" in heading:
        return "percent"
    if low.startswith(("hoeveelheid", "volume", "aantal")):
        return "number"
    if low.endswith(("tijdstip", "tijd")):
        return "time"
    return _infer_type(f"{label} {heading}")


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _infer_type(label):
    low = label.lower()
    if _has_boxes(label) or low.endswith("ja/nee") or low in YES_NO:
        return "select"
    if "handtekening" in low or "signatuur" in low:
        return "signature"
    if "%" in label or "percentage" in low:
        return "percent"
    if "datum" in low and "tijd" in low:
        return "datetime"
    if low.startswith(("datum", "tht")) or low.endswith("datum"):
        return "date"
    if low.endswith(("tijd", "tijdstip")):
        return "time"
    if low.startswith(("opmerking", "beschrijving", "oorzaak", "afwijking", "toelichting")):
        return "long_text"
    return "text"


def _field(field_type, label, options=None, unit=None, spec=None, role="input", note=None):
    return {
        "label": label,
        "field_type": field_type,
        "options": options or [],
        "unit": unit,
        "spec": spec,
        "role": role,
        "note": note,
        "required": role == "input",
    }


def _clean(text):
    text = BLANK.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" |:;,.→-")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _table(rows, caption):
    head = [_norm(c) for c in rows[0]]
    if _is_calc(head):
        return _calc(rows, caption)
    return _grid(rows, head, caption)


def _grid(rows, head, caption):
    """One field per row, with its rule and unit attached to it.

    A row is a single question even when it is spread over three columns:
    `Temperatuur | °C | ____` is not a field called Temperatuur plus a field
    called Temperatuur again.
    """
    columns = _columns(head)
    body = rows[1:] if columns else rows
    if not columns:
        columns = [(0, "actual", "")]

    fields = []
    for row in body:
        label = _clean(row[0]) if row else ""
        if not label:
            continue

        cells = {"spec": [], "actual": [], "check": []}
        for index, role, heading in columns:
            if index < len(row):
                cells[role].append((heading, _clean(row[index])))

        if cells["check"]:
            # A row whose *name* is itself a choice (`US Extractor ☐ 20L ☐ 50L`)
            # asks that choice once, then a tick per column.
            if _has_boxes(label):
                fields.extend(_choice_fields(label, caption))
            for heading, _ in cells["check"]:
                fields.append(_field("checkbox", f"{_strip_boxes(label)} - {heading}"))
            continue

        actuals = cells["actual"]
        if len([heading for heading, _ in actuals if heading]) > 1:
            # A table of measurements: every column is its own value. Taking
            # only the first would silently drop half the record.
            for heading, _ in actuals:
                if heading:
                    fields.append(_field(_type_for(heading, label), f"{label} - {heading}"))
            continue

        spec = next((cell for _, cell in cells["spec"] if cell), "")
        actual = next((cell for _, cell in actuals if cell), "")

        if _has_boxes(actual):
            fields.extend(_choice_fields(f"{label}: {actual}", label))
            continue

        if spec.lower() == "ja/nee" or actual.lower() == "ja/nee":
            fields.append(_field("select", label, options=["Ja", "Nee"]))
            if actual and actual.lower() != "ja/nee":
                fields.append(_field(_infer_type(actual), actual))
            continue

        field = _field(_infer_type(label), label)
        if spec.lower() in UNIT_HEADS:
            field["unit"] = UNITS.get(spec.lower(), spec)
        elif spec:
            field["spec"] = spec
        if actual.lower() in UNIT_HEADS:
            field["unit"] = field["unit"] or UNITS.get(actual.lower(), actual)
            actual = ""
        elif actual and _is_hint(actual):
            # `Batchnummer | (1e 3 letters ...) bv ESC-FSC-250826 | ____`
            field["note"] = actual
            actual = ""
        fields.append(field)

        # `Overnacht laten staan | Ja/nee | Duur:` -- the rule is a choice and
        # the cell beside it asks a second question.
        if actual:
            fields.append(_field(_infer_type(actual), actual))

    return {"type": "table", "label": caption, "fields": fields} if fields else None


def _columns(head):
    """[(index, role, heading)] for the value columns of a table.

    The first column names the row; what follows is a rule, a tick, or the
    place the operator writes. An unrecognised heading is still a place to
    write -- dropping it would silently lose a recorded value.
    """
    if len(head) < 2:
        return []
    candidates = head[1:]
    if not any(candidates):
        return []

    known = [c for c in candidates if c]
    # `Gegeven | In te vullen`: one value column, however the heading is worded.
    if all(c in ACTUAL_HEADS for c in known) and not any(c in SPEC_HEADS for c in known):
        return [(1, "actual", "")]

    columns = []
    for offset, name in enumerate(candidates):
        index = offset + 1
        if name in SPEC_HEADS:
            columns.append((index, "spec", name))
        elif name in CHECK_HEADS:
            columns.append((index, "check", name))
        elif name in ACTUAL_HEADS or not name:
            columns.append((index, "actual", ""))
        else:
            columns.append((index, "actual", name))
    return columns


def _is_calc(head):
    joined = " ".join(h for h in head if h)
    return "invulgegevens" in joined and "resultaat" in joined


def _calc(rows, caption):
    """`input | ____ | result | ____` rows become input/result pairs.

    The SOP hands the arithmetic to a spreadsheet, so these are typed-in
    numbers rather than computed ones: the record shows what was calculated
    and with which inputs, which is what an auditor asks for.
    """
    fields = []
    for row in rows[1:]:
        label = _clean(row[0]) if row else ""
        if not label:
            continue
        if len(row) >= 4:
            fields.append(_field("number", f"{label} - invoer"))
            result = _clean(row[2])
            if result:
                fields.append(_field("number", f"{result} - resultaat", role="result"))
        else:
            fields.append(_field("text", label))
    return {"type": "calc", "label": caption, "fields": fields} if fields else None


def _norm(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


# ---------------------------------------------------------------------------
# The document-control block
# ---------------------------------------------------------------------------


def _is_doc_control(rows):
    """The `Documentnummer | Versie | Status` block at the top of an SOP.

    It is not operator input: it repeats what the SOP record already holds and
    is printed so the paper record can be identified.
    """
    if len(rows) < 2:
        return False
    first = [_norm(c) for c in rows[0]]
    return "documentnummer" in first or "versie" in first


def _doc_control(rows):
    fields = []
    for i in range(0, len(rows) - 1, 2):
        for label, value in zip(rows[i], rows[i + 1]):
            label, value = _clean(label), _clean(value)
            if label and value:
                field = _field("text", label, role="data", note=value)
                field["required"] = False
                fields.append(field)
    return {"type": "doc_control", "label": None, "fields": fields}
