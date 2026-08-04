"""Per-org deployment configuration for Rally.

Each org (PaSE, SCPO Process Excellence, ...) gets its own Databricks App,
UC volume, and set of Workflow jobs (see `registry.py`), all built on the
same shared codebase. Pillar lists live in `orgs/<org_id>/pillars.json`.
"""
