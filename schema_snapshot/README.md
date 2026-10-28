# Schema snapshot

The six custom doctypes in this system (`Extraction Batch`, `Extraction Batch Input`,
`Harvest Batch`, `Plant Source`, `Processing Stage Log`, `SOP`) were created directly
through the Frappe REST API with `module: "Custom"`, so their definitions live only in
the live site's database. The files here are a tracked backup of that schema, plus the
Custom Fields added to native doctypes and the role permission rows.

**These files are inert.** They are deliberately *not* under
`symbiotope_traceability/doctype/`, which is the path `bench migrate` reads and syncs.
Nothing imports or applies them; they exist so the schema can be read locally and
restored if the site is ever lost.

Regenerate with the export script used to create them (a logged-in GET against
`/api/resource/DocType/<name>`). Volatile fields (`modified`, `creation`, `owner`,
`modified_by`, `docstatus`, `idx`) are stripped so diffs stay meaningful.

| File | Contents |
|---|---|
| `extraction_batch.json` etc. | one per custom doctype, full field + permission definition |
| `custom_fields.json` | Custom Fields on `Batch` (`harvest_batch`, `extraction_batch`, `lot_status`), `Plant Source` and `Item` |
| `custom_docperms.json` | all `Custom DocPerm` rows — note these *replace* native permissions for any doctype they cover |
