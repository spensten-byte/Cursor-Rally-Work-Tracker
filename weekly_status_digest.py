"""Friday morning digest: a Slack DM summarizing which pillars have not yet
submitted their weekly update, so the recipient(s) can follow up personally
rather than relying only on the per-lead reminder DMs.

Runs as a Databricks Workflows job every Friday at 8 AM Pacific. Reuses the
same "missing this week" check as reminders.py, but sends one Slack DM per
fixed recipient (RALLY_DIGEST_RECIPIENT, comma-separated for multiple)
instead of DMing each pillar's lead individually. Defaults to Spencer for
PaSE; each org can override via `extra_env` in orgs/registry.py.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

try:
    ROOT = Path(__file__).resolve().parent
except NameError:
    # Databricks serverless spark_python_task runs this file via
    # exec(compile(..., filename, 'exec')), which does not set __file__.
    # The current frame's co_filename still points at the compiled source.
    ROOT = Path(sys._getframe().f_code.co_filename).resolve().parent
sys.path.insert(0, str(ROOT))

# Set env defaults for this job's org (RALLY_ORG, default "pase") before any
# other local module imports, so get_settings()/pillars.py see the right
# volume/host/pillar-list regardless of which env vars Workflows injects.
from orgs.registry import apply_env_defaults  # noqa: E402

apply_env_defaults()

from databricks.sdk import WorkspaceClient

from config import get_settings  # noqa: E402
from pillars import PILLARS, get_pillar  # noqa: E402
from pillar_leads import load_leads  # noqa: E402
from storage import MeetingStorage  # noqa: E402
from reminders import (  # noqa: E402
    RALLY_URL,
    _current_week_window,
    _read_slack_token,
    _slack_dm,
    _slack_lookup_user_id,
)

DIGEST_RECIPIENT_EMAILS = [
    e.strip()
    for e in os.getenv("RALLY_DIGEST_RECIPIENT", "Spencer.Stendel@nike.com").split(",")
    if e.strip()
]


def _display_name_from_email(email: str) -> str:
    """Mirror app.py's helper so leads are shown by name, not email."""
    if not email or "@" not in email:
        return email
    local = email.split("@", 1)[0]
    parts = [re.sub(r"\d+$", "", p) for p in local.split(".")]
    name = " ".join(p for p in parts if p)
    return name or email


def _format_week_range(start: date, end: date) -> str:
    """Human-readable week range without year, e.g. 'Jul 27 \u2013 Aug 2'."""
    start_str = f"{start.strftime('%b')} {start.day}"
    end_str = f"{end.strftime('%b')} {end.day}"
    return f"{start_str} \u2013 {end_str}"


def main() -> int:
    settings = get_settings()
    storage = MeetingStorage(settings)
    ws = WorkspaceClient()
    slack_token = _read_slack_token(ws)

    leads: dict[str, list[str]] = load_leads(storage)

    week_start, week_end = _current_week_window()
    print(f"[rally.digest] Checking week {week_start} \u2014 {week_end}", flush=True)

    latest = storage.latest_in_range_per_pillar(week_start, week_end)
    missing_slugs = [p.slug for p in PILLARS if not latest.get(p.slug)]
    print(f"[rally.digest] Missing submissions: {missing_slugs}", flush=True)

    recipient_ids: list[str] = []
    for email in DIGEST_RECIPIENT_EMAILS:
        user_id = _slack_lookup_user_id(slack_token, email)
        if not user_id:
            print(f"[rally.digest] Could not resolve Slack user for {email}", flush=True)
            continue
        recipient_ids.append(user_id)

    if not recipient_ids:
        print("[rally.digest] No recipients resolved, nothing sent", flush=True)
        return 1

    week_range = _format_week_range(week_start, week_end)

    if not missing_slugs:
        text = (
            f"Rally Weekly Digest ({week_range}): "
            f"all {len(PILLARS)} pillars have submitted their one-pager this week."
        )
    else:
        lines = [f"Rally Weekly Digest ({week_range}) \u2014 not yet submitted:"]
        for slug in missing_slugs:
            pillar = get_pillar(slug)
            emails = leads.get(slug, [])
            lead = (
                ", ".join(_display_name_from_email(e) for e in emails)
                if emails
                else "no lead configured"
            )
            lines.append(f"\u2022 *{pillar.name}* \u2014 {lead}")
        lines.append(RALLY_URL)
        text = "\n".join(lines)

    results = [_slack_dm(slack_token, user_id, text) for user_id in recipient_ids]
    print(f"[rally.digest] Done. sent={sum(results)}/{len(results)}", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    # Note: do NOT wrap in sys.exit(). The Databricks serverless
    # spark_python_task runs this file inside an IPython kernel where
    # SystemExit is treated as a kernel abort and marks the run FAILED
    # even when the script completed successfully.
    main()
