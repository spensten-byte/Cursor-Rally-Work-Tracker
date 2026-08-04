"""Canonical list of pillars for the active org's Rally deployment.

This is the single source of truth for pillar names, slugs (used for storage
paths and session-state keys), short labels (used for tab headers, since
full pillar names are too long for the Streamlit tab strip), and the
`prompt_label` used by `prompt_roster_sync.py` to find this pillar's roster
block in the org's `extract.md` prompt override.

The pillar list is loaded from a JSON file (`RALLY_PILLARS_CONFIG` env var,
set by `orgs/registry.py` per org). If unset or unreadable, falls back to
PaSE's original hardcoded 7-pillar list embedded below, so any deployment
that predates this loader keeps working with zero config changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Pillar:
    slug: str
    name: str
    short: str
    prompt_label: str = ""


# PaSE's original list, kept as the built-in default so deployments that
# don't set RALLY_PILLARS_CONFIG (or whose config file is missing) see
# exactly today's behavior.
_DEFAULT_PILLARS: list[Pillar] = [
    Pillar("demand-excellence", "Demand Excellence", "Demand", "Demand"),
    Pillar("supply-excellence", "Supply Excellence", "Supply", "Supply"),
    Pillar(
        "order-capture",
        "Order Capture & Promise Excellence",
        "Order Capture",
        "Order Capture & Promise",
    ),
    Pillar(
        "inventory-fulfillment",
        "Inventory Deployment & Fulfillment Excellence",
        "Inventory & Fulfillment",
        "Inventory & Fulfillment",
    ),
    Pillar("distribution-excellence", "Distribution Excellence", "Distribution", "Distribution"),
    Pillar("process-intelligence", "Process Intelligence", "Process Intel", "Process Intelligence"),
    Pillar("process-enablement", "Process Enablement", "Process Enable", "Process Enablement"),
]

DEFAULT_PILLAR_SLUG = "process-intelligence"


def _load_pillars() -> list[Pillar]:
    config_path = os.getenv("RALLY_PILLARS_CONFIG", "")
    if not config_path:
        return _DEFAULT_PILLARS
    path = Path(config_path)
    if not path.is_absolute():
        path = PACKAGE_DIR / path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        pillars = [
            Pillar(
                slug=entry["slug"],
                name=entry["name"],
                short=entry.get("short", entry["name"]),
                prompt_label=entry.get("prompt_label", ""),
            )
            for entry in raw
        ]
        if pillars:
            return pillars
    except Exception:
        pass
    return _DEFAULT_PILLARS


PILLARS: list[Pillar] = _load_pillars()

_BY_SLUG: dict[str, Pillar] = {p.slug: p for p in PILLARS}
_FALLBACK_SLUG = DEFAULT_PILLAR_SLUG if DEFAULT_PILLAR_SLUG in _BY_SLUG else PILLARS[0].slug


def get_pillar(slug: str) -> Pillar:
    """Return the pillar matching `slug`, falling back to the default if unknown."""
    return _BY_SLUG.get(slug, _BY_SLUG[_FALLBACK_SLUG])


def all_slugs() -> list[str]:
    return [p.slug for p in PILLARS]
