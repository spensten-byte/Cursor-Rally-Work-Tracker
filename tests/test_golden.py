"""Golden tests for executive one-pager rendering (no LLM required)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from summarizer import MeetingSummarizer, REQUIRED_KEYS

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def summarizer() -> MeetingSummarizer:
    return MeetingSummarizer()


@pytest.fixture
def may6_extract() -> dict:
    return json.loads((FIXTURES / "may6_extract.json").read_text(encoding="utf-8"))


def test_validate_extract_requires_keys(may6_extract: dict) -> None:
    validated = MeetingSummarizer.validate_extract(may6_extract)
    assert REQUIRED_KEYS.issubset(validated.keys())


def test_may6_render_contains_executive_sections(
    summarizer: MeetingSummarizer, may6_extract: dict
) -> None:
    result = summarizer.summarize_from_extract(may6_extract)
    md = result.markdown.lower()
    html = result.html.lower()

    assert "pase distribution weekly" in md
    assert "## wins / closures" in md
    assert "sto us→ca" in md or "sto us" in md
    assert "## reassignments" in md
    assert "skims rtv" in md
    assert "## watch list" in md
    assert "110%" in md or "110" in md
    assert "309 movement" in md

    assert "watch list" in html
    assert "closures" in html or "wins" in html


def test_may6_render_omits_empty_sections(summarizer: MeetingSummarizer) -> None:
    minimal = {
        "meeting_date": "2026-01-01",
        "meeting_title": "Standup",
        "decisions": [],
        "closures": [],
        "new_tracks": [],
        "reassignments": [],
        "watch_list": [],
        "gaps": ["No blockers reported"],
        "next_steps": [],
    }
    result = summarizer.summarize_from_extract(minimal)
    assert "## Decisions" not in result.markdown
    assert "## Gaps" in result.markdown or "gaps" in result.markdown.lower()


def test_notes_fixture_loads() -> None:
    notes = (FIXTURES / "may6_notes.txt").read_text(encoding="utf-8")
    assert "PASE Distribution" in notes
    assert "Manu" in notes
    assert len(notes) > 500
