"""One-time backfill: push all historical one-pagers and leadership rollups
that predate Box mirroring (or predate `box_mirror_enabled`) into Box, using
the same weekly-folder structure `box_mirror.py` now uses for new writes:

    {root}/One-Pagers/Week of {end-of-week-friday}/{pillar-slug}/{record_id}.md
    {root}/Leadership-Rollups/Week of {end-of-week-friday}/{kind}_{record_id}.md

Run this from a Databricks notebook (NOT locally — same reason as
`scripts/box_smoketest.py`: Box's API is not reachable from behind a
corporate proxy that does TLS interception, which is the case on
Nike-managed laptops):

    %pip install "boxsdk[jwt]>=10"
    dbutils.library.restartPython()

    # then, in a NEW cell (packages only load after the restart):
    # %run this file, or paste its contents into a cell, then call main().

This is a standalone, throwaway script. It is not imported by the Rally app
and touches no app state — it only reads from the UC Volume (via
`storage.MeetingStorage`, exactly like the running app does) and uploads to
Box (via `box_mirror.py`'s internal `_do_mirror_*` functions, called
directly and synchronously rather than through the interactive
12s-bounded/async wrapper the app uses on every Submit click — a batch job
can afford to block on each upload and retry on failure).

Uploads are idempotent: `box_mirror._upload_markdown()` already falls back
to `upload_file_version` on a Box `item_name_in_use` conflict, so re-running
this script after a partial failure is safe.

--- Scoping the run ---

Edit `PILLAR_FILTER` and `INCLUDE_ROLLUPS` below before each run:

  - Pilot run (first pillar only, no rollups):
        PILLAR_FILTER = all_slugs()[0]
        INCLUDE_ROLLUPS = False

  - Full run (once the pilot is verified in Box):
        PILLAR_FILTER = None
        INCLUDE_ROLLUPS = True
"""

from __future__ import annotations

from datetime import datetime, timezone

import box_mirror
from pillars import all_slugs, get_pillar
from storage import MeetingStorage

# ── Scope for this run — edit before each execution, see docstring above ──
PILLAR_FILTER: str | None = None  # None = all pillars
INCLUDE_ROLLUPS: bool = True


def _parse_created_at(created_at: str) -> datetime:
    """Parse a record's stored `created_at` (UTC ISO 8601) back to a datetime.
    Falls back to "now" if a record is somehow missing it, so a single bad
    record can't crash the whole backfill."""
    if not created_at:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(created_at)
    except ValueError:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def backfill_summaries(storage: MeetingStorage, sdk_client, pillar_filter: str | None) -> tuple[int, int, list[str]]:
    """Upload every archived one-pager for the targeted pillar(s). Returns
    (uploaded, skipped, failed_record_ids)."""
    uploaded = 0
    skipped = 0
    failed: list[str] = []

    slugs = [pillar_filter] if pillar_filter else all_slugs()
    for slug in slugs:
        pillar_name = get_pillar(slug).name
        records = storage.list_summaries(limit=10_000, pillar=slug)
        print(f"[box_backfill] {pillar_name} ({slug}): {len(records)} one-pager(s) found", flush=True)
        for record in records:
            try:
                markdown = storage.read_file(record.markdown_path)
                effective_dt = _parse_created_at(record.created_at)
                box_mirror._do_mirror_summary(
                    sdk_client, storage.settings, record.id, slug, markdown, effective_dt
                )
                uploaded += 1
            except Exception as exc:
                print(f"[box_backfill] FAILED summary {slug}/{record.id}: {exc}", flush=True)
                failed.append(f"{slug}/{record.id}")
    return uploaded, skipped, failed


def backfill_rollups(storage: MeetingStorage, sdk_client) -> tuple[int, int, list[str]]:
    """Upload every archived leadership rollup (team + org). Returns
    (uploaded, skipped, failed_record_ids)."""
    uploaded = 0
    skipped = 0
    failed: list[str] = []

    for kind in ("team", "org"):
        records = storage.list_rollups(limit=10_000, kind=kind)
        print(f"[box_backfill] rollups ({kind}): {len(records)} found", flush=True)
        for record in records:
            try:
                markdown = storage.read_file(record.markdown_path)
                effective_dt = _parse_created_at(record.created_at)
                box_mirror._do_mirror_rollup(
                    sdk_client, storage.settings, record.id, kind, markdown, effective_dt
                )
                uploaded += 1
            except Exception as exc:
                print(f"[box_backfill] FAILED rollup {kind}/{record.id}: {exc}", flush=True)
                failed.append(f"{kind}/{record.id}")
    return uploaded, skipped, failed


def main() -> None:
    storage = MeetingStorage()
    sdk_client = storage._sdk_client()

    if not storage.settings.box_mirror_enabled or not storage.settings.box_root_folder_id:
        print("[box_backfill] box_mirror_enabled/box_root_folder_id not set — nothing to do.", flush=True)
        return

    scope = PILLAR_FILTER or "ALL PILLARS"
    print(f"[box_backfill] starting — pillar scope: {scope}, rollups included: {INCLUDE_ROLLUPS}", flush=True)

    total_uploaded = 0
    total_failed: list[str] = []

    s_uploaded, _s_skipped, s_failed = backfill_summaries(storage, sdk_client, PILLAR_FILTER)
    total_uploaded += s_uploaded
    total_failed += s_failed

    if INCLUDE_ROLLUPS:
        r_uploaded, _r_skipped, r_failed = backfill_rollups(storage, sdk_client)
        total_uploaded += r_uploaded
        total_failed += r_failed

    print("=" * 60, flush=True)
    print(f"[box_backfill] DONE — uploaded: {total_uploaded}, failed: {len(total_failed)}", flush=True)
    if total_failed:
        print(f"[box_backfill] failed record ids (retry-able, safe to re-run): {total_failed}", flush=True)


if __name__ == "__main__":
    main()
