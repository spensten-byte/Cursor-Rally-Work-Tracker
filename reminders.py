"""Weekly Slack reminder for pillar leads who haven't submitted yet.

Runs as a Databricks Workflows job every Thursday at 9 AM Pacific. Reads the
current week's submissions from UC Volume; for every pillar with no submission,
looks up the lead's Slack user ID by email and sends a DM reminding them to
submit their one-pager in Rally.
"""
from __future__ import annotations

import base64
import os
import sys
from datetime import date, timedelta
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
# Without this, DATABRICKS_HOST is empty on job compute, MeetingStorage
# silently falls back to an empty local directory, and every pillar looks
# unsubmitted every week.
from orgs.registry import apply_env_defaults  # noqa: E402

apply_env_defaults()

import requests
from databricks.sdk import WorkspaceClient

from config import get_settings  # noqa: E402
from pillars import PILLARS, get_pillar  # noqa: E402
from pillar_leads import load_leads  # noqa: E402
from storage import MeetingStorage  # noqa: E402
from time_utils import pacific_today  # noqa: E402

RALLY_URL = os.getenv(
    "RALLY_APP_URL", "https://pase-work-tracker-7474649843005973.aws.databricksapps.com/"
)
SECRET_SCOPE = "pase-work-tracker"
SECRET_KEY = "slack-bot-token"


def _current_week_window(today: date | None = None) -> tuple[date, date]:
    """Return (Monday, Sunday) for the ISO week containing `today`."""
    today = today or pacific_today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _read_slack_token(ws: WorkspaceClient) -> str:
    """Read the Slack bot token from the Databricks secret scope."""
    try:
        return ws.dbutils.secrets.get(scope=SECRET_SCOPE, key=SECRET_KEY)
    except Exception:
        response = ws.secrets.get_secret(scope=SECRET_SCOPE, key=SECRET_KEY)
        return base64.b64decode(response.value).decode("utf-8")


def _slack_lookup_user_id(token: str, email: str) -> str | None:
    r = requests.get(
        "https://slack.com/api/users.lookupByEmail",
        headers={"Authorization": f"Bearer {token}"},
        params={"email": email},
        timeout=15,
    )
    data = r.json()
    if not data.get("ok"):
        print(
            f"[rally.reminder] users.lookupByEmail failed for {email}: "
            f"{data.get('error')}",
            flush=True,
        )
        return None
    return data["user"]["id"]


def _slack_dm(token: str, user_id: str, text: str) -> bool:
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={"channel": user_id, "text": text},
        timeout=15,
    )
    data = r.json()
    if not data.get("ok"):
        print(
            f"[rally.reminder] chat.postMessage failed for {user_id}: "
            f"{data.get('error')}",
            flush=True,
        )
        return False
    return True


def main() -> int:
    settings = get_settings()
    storage = MeetingStorage(settings)
    ws = WorkspaceClient()
    slack_token = _read_slack_token(ws)

    leads: dict[str, list[str]] = load_leads(storage)

    week_start, week_end = _current_week_window()
    print(
        f"[rally.reminder] Checking week {week_start} \u2014 {week_end}",
        flush=True,
    )

    latest = storage.latest_in_range_per_pillar(week_start, week_end)
    missing_slugs = [p.slug for p in PILLARS if not latest.get(p.slug)]
    print(
        f"[rally.reminder] Missing submissions: {missing_slugs}",
        flush=True,
    )

    sent, skipped, failed = 0, 0, 0
    for slug in missing_slugs:
        pillar = get_pillar(slug)
        emails = [e for e in leads.get(slug, []) if not e.upper().startswith("TBD")]
        if not emails:
            print(
                f"[rally.reminder] Skipping {slug} \u2014 no lead email configured",
                flush=True,
            )
            skipped += 1
            continue
        for email in emails:
            user_id = _slack_lookup_user_id(slack_token, email)
            if not user_id:
                failed += 1
                continue
            text = (
                f"Hi! It's Thursday \u2014 a friendly reminder to submit this week's "
                f"*{pillar.name}* one-pager in Rally.\n{RALLY_URL}"
            )
            if _slack_dm(slack_token, user_id, text):
                sent += 1
                print(
                    f"[rally.reminder] DM sent to {email} for pillar {slug}",
                    flush=True,
                )
            else:
                failed += 1

    print(
        f"[rally.reminder] Done. sent={sent} skipped={skipped} failed={failed}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    # Note: do NOT wrap in sys.exit(). The Databricks serverless
    # spark_python_task runs this file inside an IPython kernel where
    # SystemExit is treated as a kernel abort and marks the run FAILED
    # even when the script completed successfully.
    main()
