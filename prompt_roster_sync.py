"""Best-effort sync of a pillar's roster into the live `extract` prompt's
PILLAR ROSTER block, triggered when a lead adds/removes team members via the
Team tab. Only the `Members:` bullet list for the changed pillar is touched —
Director lines, other pillars' rosters, and the rest of the prompt are left
untouched.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pillars import get_pillar

if TYPE_CHECKING:
    from summarizer import MeetingSummarizer

PROMPT_NAME = "extract"


class RosterSyncError(RuntimeError):
    """Raised when this pillar's roster block can't be located in the prompt.
    The prompt is left completely untouched whenever this is raised — callers
    must surface this to the lead rather than swallow it silently."""


def sync_pillar_roster(
    summarizer: "MeetingSummarizer", pillar_slug: str, names: list[str], edited_by: str
) -> None:
    # The exact "Pillar: <label>" text used in the hand-authored PILLAR ROSTER
    # block inside this org's prompts_overrides/extract.md. Worded differently
    # from pillars.py's name/short fields (e.g. "Order Capture & Promise" vs.
    # "Order Capture & Promise Excellence"), so it's kept as its own field
    # (`prompt_label`) in each org's pillars.json rather than derived.
    label = get_pillar(pillar_slug).prompt_label
    if not label:
        raise RosterSyncError(f"No prompt-roster label mapped for pillar '{pillar_slug}'.")

    text = summarizer.load_prompt(PROMPT_NAME)
    pattern = re.compile(
        rf"(Pillar: {re.escape(label)}\nDirector:[^\n]*\nMembers:\n)(.*?)(?=\n\n|\}})",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise RosterSyncError(
            f"Could not find the '{label}' section in the extract prompt's "
            "PILLAR ROSTER block — the prompt was NOT changed. Update it manually."
        )

    new_block = "\n".join(f"- {n}" for n in names)
    new_text = text[: match.start(2)] + new_block + text[match.end(2):]
    if new_text == text:
        return  # names unchanged — skip an unnecessary write + audit entry
    summarizer.save_prompt(PROMPT_NAME, new_text, edited_by)
