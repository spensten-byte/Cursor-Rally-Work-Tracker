"""Canonical list of PaSE pillars used across the app.

This is the single source of truth for pillar names, slugs (used for storage
paths and session-state keys), and short labels (used for tab headers, since
full pillar names are too long for the Streamlit tab strip).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pillar:
    slug: str
    name: str
    short: str


PILLARS: list[Pillar] = [
    Pillar("demand-excellence", "Demand Excellence", "Demand"),
    Pillar("supply-excellence", "Supply Excellence", "Supply"),
    Pillar("order-capture", "Order Capture & Promise Excellence", "Order Capture"),
    Pillar(
        "inventory-fulfillment",
        "Inventory Deployment & Fulfillment Excellence",
        "Inventory & Fulfillment",
    ),
    Pillar("distribution-excellence", "Distribution Excellence", "Distribution"),
    Pillar("process-intelligence", "Process Intelligence", "Process Intel"),
    Pillar("process-enablement", "Process Enablement", "Process Enable"),
]

DEFAULT_PILLAR_SLUG = "process-intelligence"

_BY_SLUG: dict[str, Pillar] = {p.slug: p for p in PILLARS}


def get_pillar(slug: str) -> Pillar:
    """Return the pillar matching `slug`, falling back to the default if unknown."""
    return _BY_SLUG.get(slug, _BY_SLUG[DEFAULT_PILLAR_SLUG])


def all_slugs() -> list[str]:
    return [p.slug for p in PILLARS]
