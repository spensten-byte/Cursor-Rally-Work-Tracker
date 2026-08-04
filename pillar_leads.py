"""Per-org pillar leads + leadership rollup recipients, persisted on the UC
volume so each org (PaSE, SCPO, ...) manages its own list without touching
the shared codebase.

Schema (`pillar_leads.json`): `{slug: "email"}` or `{slug: ["email", ...]}`
— a pillar may have one or several leads (e.g. co-leads). `load_leads()`
always normalizes to `{slug: [emails]}`.

For backward compatibility during the migration off bundled repo files,
both loaders fall back to the bundled `data/*.json` in this repo if the
volume copy doesn't exist yet (e.g. a brand-new org that hasn't been
seeded). Once an org's volume file exists, it always wins.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pillars import all_slugs

if TYPE_CHECKING:
    from storage import MeetingStorage

LEADS_FILENAME = "pillar_leads.json"
RECIPIENTS_FILENAME = "leadership_recipients.json"

_BUNDLED_DIR = Path(__file__).resolve().parent / "data"


def _volume_path(storage: "MeetingStorage", filename: str) -> str:
    if storage._use_volume:
        return f"{storage.settings.volume_path}/{filename}"
    return str(storage._local_root() / filename)


def _normalize_leads(raw: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for slug in all_slugs():
        value = raw.get(slug, [])
        if isinstance(value, str):
            emails = [value] if value.strip() else []
        elif isinstance(value, list):
            emails = [v.strip() for v in value if isinstance(v, str) and v.strip()]
        else:
            emails = []
        out[slug] = emails
    return out


def load_leads(storage: "MeetingStorage") -> dict[str, list[str]]:
    """Return {slug: [lead emails]}. Falls back to the bundled repo file
    (data/pillar_leads.json) if this org's volume copy doesn't exist yet."""
    try:
        raw_bytes = storage._read_bytes(_volume_path(storage, LEADS_FILENAME))
        raw = json.loads(raw_bytes.decode("utf-8")) or {}
        return _normalize_leads(raw)
    except Exception:
        pass
    try:
        raw = json.loads((_BUNDLED_DIR / LEADS_FILENAME).read_text(encoding="utf-8")) or {}
        return _normalize_leads(raw)
    except Exception:
        return _normalize_leads({})


def save_leads(storage: "MeetingStorage", data: dict[str, list[str]]) -> None:
    cleaned = _normalize_leads(data)
    storage._write_bytes(
        _volume_path(storage, LEADS_FILENAME), json.dumps(cleaned, indent=2).encode("utf-8")
    )


def load_recipients(storage: "MeetingStorage") -> list[str]:
    """Return the list of leadership-rollup DM recipients for this org."""
    try:
        raw_bytes = storage._read_bytes(_volume_path(storage, RECIPIENTS_FILENAME))
        raw = json.loads(raw_bytes.decode("utf-8")) or {}
        leaders = raw.get("leaders", [])
        return [e.strip() for e in leaders if isinstance(e, str) and e.strip()]
    except Exception:
        pass
    try:
        raw = json.loads((_BUNDLED_DIR / RECIPIENTS_FILENAME).read_text(encoding="utf-8")) or {}
        leaders = raw.get("leaders", [])
        return [e.strip() for e in leaders if isinstance(e, str) and e.strip()]
    except Exception:
        return []


def save_recipients(storage: "MeetingStorage", recipients: list[str]) -> None:
    cleaned = [e.strip() for e in recipients if e.strip()]
    storage._write_bytes(
        _volume_path(storage, RECIPIENTS_FILENAME),
        json.dumps({"leaders": cleaned}, indent=2).encode("utf-8"),
    )
