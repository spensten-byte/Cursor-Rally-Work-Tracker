"""Per-pillar team roster (display names only), persisted on the UC volume."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pillars import all_slugs

if TYPE_CHECKING:
    from storage import MeetingStorage

FILENAME = "pillar_members.json"


def _path(storage: "MeetingStorage") -> str:
    if storage._use_volume:
        return f"{storage.settings.volume_path}/{FILENAME}"
    return str(storage._local_root() / FILENAME)


def load_members(storage: "MeetingStorage") -> dict[str, list[str]]:
    """Return {slug: [names]}. Missing file => all empty."""
    try:
        raw = storage._read_bytes(_path(storage))
        data = json.loads(raw.decode("utf-8")) or {}
    except Exception:
        data = {}
    return {
        slug: sorted({n.strip() for n in data.get(slug, []) if n.strip()}, key=str.lower)
        for slug in all_slugs()
    }


def save_members(storage: "MeetingStorage", data: dict[str, list[str]]) -> None:
    cleaned = {
        slug: sorted({n.strip() for n in names if n.strip()}, key=str.lower)
        for slug, names in data.items()
    }
    storage._write_bytes(_path(storage), json.dumps(cleaned, indent=2).encode("utf-8"))
