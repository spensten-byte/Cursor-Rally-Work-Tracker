"""Best-effort mirror of Rally submissions into the production Box folder.

This module is purely additive. Unity Catalog Volume (via `storage.py`'s
`MeetingStorage`) remains the canonical, authoritative store — nothing here
is ever read back by the app. `save_summary` / `save_rollup` call the
functions below as a side effect *after* their UC Volume write already
succeeded. Every failure mode (missing secrets, Box API errors, network
issues, the `boxsdk` package not being installed) is caught here and only
logged — a Box outage never blocks, delays past a bounded timeout, or fails
a user's submission.

Built against `boxsdk>=10` / the `box_sdk_gen` import namespace (Box
consolidated its Python SDKs in 2025; the classic `from boxsdk import
JWTAuth, Client` API no longer exists in current releases — see
https://github.com/box/box-python-sdk/blob/main/migration-guides/from-boxsdk-to-box_sdk_gen.md).

Folder layout under the configured root folder (see `config.py`,
`PASE_TRACKER_BOX_ROOT_FOLDER_ID`):

    {root}/One-Pagers/Week of {end-of-week-friday}/{pillar-slug}/{record_id}.md
    {root}/Leadership-Rollups/Week of {end-of-week-friday}/{kind}_{record_id}.md

The week folder groups records by Rally's existing Saturday-9am-PT work week
(see `storage._week_anchor`), labeled by the Friday that ends it — e.g. a
record saved any time in the work week starting Saturday 2026-07-11 lands
under "Week of 2026-07-17".

`record_id` already encodes a sortable UTC timestamp + a title slug (see
`MeetingStorage._stamp` / `_slug`), so filenames sort chronologically and a
future reader (e.g. an "Ask Rally" bot reading straight from Box) can list a
pillar's folder and take the lexicographically-last file to get the latest
submission without needing any extra metadata lookup.
"""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings

SECRET_SCOPE = "pase-work-tracker"
ONE_PAGERS_DIR = "One-Pagers"
ROLLUPS_DIR = "Leadership-Rollups"

# Bounded wait for the interactive app path so a Box outage adds at most this
# much latency to a Submit click. If exceeded, the upload keeps running in
# the background thread (this process is long-lived) instead of being
# abandoned. Batch jobs (rollups_job.py) block on process exit until pending
# thread-pool work finishes regardless of this timeout (Python's
# concurrent.futures registers an atexit hook for this), so nothing is lost
# there either.
_TIMEOUT_SECONDS = 12

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="box-mirror")

_client_lock = threading.Lock()
_client: Any = None  # cached BoxClient, built lazily on first use

_folder_cache_lock = threading.Lock()
_folder_cache: dict[tuple[str, str], str] = {}  # (parent_id, name) -> folder_id


def _read_secret(ws, key: str) -> str:
    try:
        return ws.dbutils.secrets.get(scope=SECRET_SCOPE, key=key)
    except Exception:
        import base64

        response = ws.secrets.get_secret(scope=SECRET_SCOPE, key=key)
        return base64.b64decode(response.value).decode("utf-8")


def _get_client(sdk_client) -> Any:
    """Return a cached BoxClient, authenticating on first call.

    `sdk_client` is an already-authenticated `databricks.sdk.WorkspaceClient`
    (reused from `MeetingStorage._sdk_client()` so this module doesn't need
    to duplicate the PAT-vs-OAuth-M2M auth resolution logic).
    """
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        from box_sdk_gen import BoxClient, BoxJWTAuth, JWTConfig  # deferred: keeps boxsdk optional for local dev

        jwt_config = JWTConfig(
            client_id=_read_secret(sdk_client, "box-prod-client-id"),
            client_secret=_read_secret(sdk_client, "box-prod-client-secret"),
            jwt_key_id=_read_secret(sdk_client, "box-prod-public-key-id"),
            private_key=_read_secret(sdk_client, "box-prod-private-key"),
            private_key_passphrase=_read_secret(sdk_client, "box-prod-passphrase"),
            enterprise_id=_read_secret(sdk_client, "box-prod-enterprise-id"),
        )
        auth = BoxJWTAuth(config=jwt_config)
        _client = BoxClient(auth=auth)
        return _client


def _conflict_id(err) -> str | None:
    """Extract the existing item's id from a Box `item_name_in_use` 409 error.

    Box's own examples are inconsistent about whether `conflicts` is a dict
    or a one-item list, so handle both shapes defensively.
    """
    body = getattr(err.response_info, "body", None) or {}
    conflicts = body.get("context_info", {}).get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        return conflicts[0].get("id")
    if isinstance(conflicts, dict):
        return conflicts.get("id")
    return None


def _ensure_subfolder(client, parent_id: str, name: str) -> str:
    """Return the id of `name` under `parent_id`, creating it if missing."""
    cache_key = (parent_id, name)
    with _folder_cache_lock:
        cached = _folder_cache.get(cache_key)
    if cached:
        return cached

    from box_sdk_gen import BoxAPIError, CreateFolderParent

    try:
        folder = client.folders.create_folder(name, CreateFolderParent(id=parent_id))
        folder_id = folder.id
    except BoxAPIError as exc:
        body = getattr(exc.response_info, "body", None) or {}
        if body.get("code") == "item_name_in_use":
            existing_id = _conflict_id(exc)
            if not existing_id:
                raise
            folder_id = existing_id
        else:
            raise

    with _folder_cache_lock:
        _folder_cache[cache_key] = folder_id
    return folder_id


def _upload_markdown(client, folder_id: str, filename: str, content: str) -> None:
    from box_sdk_gen import BoxAPIError, UploadFileAttributes, UploadFileAttributesParentField, UploadFileVersionAttributes

    data = content.encode("utf-8")
    try:
        client.uploads.upload_file(
            UploadFileAttributes(name=filename, parent=UploadFileAttributesParentField(id=folder_id)),
            io.BytesIO(data),
        )
    except BoxAPIError as exc:
        body = getattr(exc.response_info, "body", None) or {}
        if body.get("code") != "item_name_in_use":
            raise
        existing_id = _conflict_id(exc)
        if not existing_id:
            raise
        client.uploads.upload_file_version(
            existing_id,
            UploadFileVersionAttributes(name=filename),
            io.BytesIO(data),
        )


def _week_folder_label(effective_dt: datetime) -> str:
    """Return "Week of {YYYY-MM-DD}" for the Friday ending the Rally work week
    (Saturday 9am PT through the following Saturday 8:59:59am PT) that
    `effective_dt` falls in.

    Deferred import of `storage._week_anchor` avoids a circular import:
    `storage.py` imports this module at load time, so importing `storage`
    back at this module's top level would fail.
    """
    from storage import _week_anchor

    friday = (_week_anchor(effective_dt) + timedelta(days=6)).date()
    return f"Week of {friday.isoformat()}"


def _do_mirror_summary(
    sdk_client, settings: Settings, record_id: str, pillar_slug: str, markdown: str, effective_dt: datetime
) -> None:
    client = _get_client(sdk_client)
    root_id = settings.box_root_folder_id
    one_pagers_id = _ensure_subfolder(client, root_id, ONE_PAGERS_DIR)
    week_id = _ensure_subfolder(client, one_pagers_id, _week_folder_label(effective_dt))
    pillar_folder_id = _ensure_subfolder(client, week_id, pillar_slug)
    _upload_markdown(client, pillar_folder_id, f"{record_id}.md", markdown)
    print(
        f"[rally.box_mirror] mirrored summary {record_id} -> "
        f"One-Pagers/{_week_folder_label(effective_dt)}/{pillar_slug}/",
        flush=True,
    )


def _do_mirror_rollup(
    sdk_client, settings: Settings, record_id: str, kind: str, markdown: str, effective_dt: datetime
) -> None:
    client = _get_client(sdk_client)
    root_id = settings.box_root_folder_id
    rollups_id = _ensure_subfolder(client, root_id, ROLLUPS_DIR)
    week_id = _ensure_subfolder(client, rollups_id, _week_folder_label(effective_dt))
    _upload_markdown(client, week_id, f"{kind}_{record_id}.md", markdown)
    print(
        f"[rally.box_mirror] mirrored rollup {record_id} -> "
        f"Leadership-Rollups/{_week_folder_label(effective_dt)}/",
        flush=True,
    )


def _run_with_timeout(label: str, fn, *args) -> None:
    future = _executor.submit(fn, *args)
    try:
        future.result(timeout=_TIMEOUT_SECONDS)
    except _FutureTimeoutError:
        print(
            f"[rally.box_mirror] {label} still running past {_TIMEOUT_SECONDS}s; "
            "continuing in the background, not blocking the caller",
            flush=True,
        )
    except Exception as exc:
        print(f"[rally.box_mirror] {label} failed (UC Volume save is unaffected): {exc}", flush=True)


def mirror_summary(
    sdk_client,
    settings: Settings,
    record_id: str,
    pillar_slug: str,
    markdown: str,
    effective_dt: datetime | None = None,
) -> None:
    """Best-effort: push a one-pager's markdown to Box. Never raises."""
    if not settings.box_mirror_enabled or not settings.box_root_folder_id:
        return
    _run_with_timeout(
        f"summary {record_id}",
        _do_mirror_summary,
        sdk_client,
        settings,
        record_id,
        pillar_slug,
        markdown,
        effective_dt or datetime.now(timezone.utc),
    )


def mirror_rollup(
    sdk_client,
    settings: Settings,
    record_id: str,
    kind: str,
    markdown: str,
    effective_dt: datetime | None = None,
) -> None:
    """Best-effort: push a leadership rollup's markdown to Box. Never raises."""
    if not settings.box_mirror_enabled or not settings.box_root_folder_id:
        return
    _run_with_timeout(
        f"rollup {record_id}",
        _do_mirror_rollup,
        sdk_client,
        settings,
        record_id,
        kind,
        markdown,
        effective_dt or datetime.now(timezone.utc),
    )
