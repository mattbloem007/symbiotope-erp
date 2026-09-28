# Symbiotope Traceability

Custom Frappe app providing the automation for Symbiotope's botanical
production traceability system (Milestone 4): auto-creating Raw Material
and Extract lots + Stock Entries as batches move through processing and
extraction, and gating extraction inputs to released material only.

Exists as a real app (rather than Server Scripts) because Frappe Cloud
disables Server Scripts on shared/public bench groups as of v15 — this
ships the same logic as proper `hooks.py` `doc_events`, installable via
Frappe Cloud's "Add App from GitHub" flow without needing a private bench.

## Install

On the Frappe Cloud dashboard: Site → Apps → Add App → paste this repo's
GitHub URL → deploy.
