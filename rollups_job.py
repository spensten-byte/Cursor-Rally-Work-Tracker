"""Weekly leadership rollup job — generates Team + Org rollups and DMs them.

Runs as a Databricks Workflows job every Friday at 9 AM Pacific. Reads the
current week's pillar submissions from UC Volume, generates both the Team Level
and Org Level leadership rollups (using the same LLM + templates as the Rally
UI), saves them to history so they appear in the Leadership Rollup tab, then
DMs each leader in data/leadership_recipients.json a summary message with four
editable attachments: Team .md, Team .html, Org .md, Org .html.
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
    ROOT = Path(sys._getframe().f_code.co_filename).resolve().parent
sys.path.insert(0, str(ROOT))

# Set env defaults for this job's org (RALLY_ORG, default "pase") before any
# other local module imports, so get_settings()/pillars.py see the right
# volume/host/pillar-list regardless of which env vars Workflows injects.
from orgs.registry import apply_env_defaults  # noqa: E402

apply_env_defaults()

import requests
from databricks.sdk import WorkspaceClient

from config import get_settings  # noqa: E402
from pillars import PILLARS, get_pillar  # noqa: E402
from pillar_leads import load_recipients  # noqa: E402
from storage import MeetingStorage  # noqa: E402
from summarizer import MeetingSummarizer  # noqa: E402
from time_utils import pacific_today  # noqa: E402

RALLY_URL      = os.getenv(
    "RALLY_APP_URL", "https://pase-work-tracker-7474649843005973.aws.databricksapps.com/"
)
SECRET_SCOPE   = "pase-work-tracker"
SECRET_KEY     = "slack-bot-token"
SECRET_KEY_PAT = "databricks-token"


# ── Date window ──────────────────────────────────────────────────────────────

def _current_work_week(today: date | None = None) -> tuple[date, date]:
    """Return (Monday, Friday) of the ISO week containing `today`.

    Matches the Leadership Rollup UI default at app.py:1486-1493.
    """
    today = today or pacific_today()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


# ── Pillar summaries ─────────────────────────────────────────────────────────

def _build_pillar_summaries(
    storage: MeetingStorage,
    latest: dict,
) -> list[dict]:
    summaries = []
    for slug, record in latest.items():
        if record is None:
            continue
        md = storage.read_file(record.markdown_path)
        summaries.append({"pillar": get_pillar(slug).name, "markdown": md})
    return summaries


# ── DM formatting ────────────────────────────────────────────────────────────

def _excerpt(md: str, max_chars: int = 1200) -> str:
    """Keep the top of the markdown so the DM body is scannable but not noisy."""
    lines, out, total = md.splitlines(), [], 0
    for line in lines:
        if total + len(line) > max_chars and out:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out).strip()


def _format_dm_text(week_start: date, week_end: date, team_md: str, org_md: str) -> str:
    rng = f"{week_start.strftime('%b %-d')} \u2013 {week_end.strftime('%b %-d, %Y')}"
    return (
        f"*Weekly Rally Rollup \u2014 {rng}*\n\n"
        f"Team-level and Org-level rollups are attached below. Each is provided "
        f"in both Markdown (`.md`, edit in any text editor) and HTML (`.html`, "
        f"open in a browser for the styled view).\n\n"
        f"*Team Level \u2014 at a glance*\n{_excerpt(team_md)}\n\n"
        f"*Org Level \u2014 at a glance*\n{_excerpt(org_md)}"
    )


# ── Slack helpers ────────────────────────────────────────────────────────────

def _read_slack_token(ws: WorkspaceClient) -> str:
    """Read the Slack bot token from the Databricks secret scope."""
    try:
        return ws.dbutils.secrets.get(scope=SECRET_SCOPE, key=SECRET_KEY)
    except Exception:
        response = ws.secrets.get_secret(scope=SECRET_SCOPE, key=SECRET_KEY)
        return base64.b64decode(response.value).decode("utf-8")


def _read_databricks_pat(ws: WorkspaceClient) -> str:
    """Read the Databricks PAT used for LLM serving-endpoint auth.

    Mirrors how app.yaml injects PASE_TRACKER_PAT into the Streamlit app, but
    works in a Workflows job where automatic secret-to-env binding is not
    available.
    """
    try:
        return ws.dbutils.secrets.get(scope=SECRET_SCOPE, key=SECRET_KEY_PAT)
    except Exception:
        response = ws.secrets.get_secret(scope=SECRET_SCOPE, key=SECRET_KEY_PAT)
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
            f"[rally.rollup] users.lookupByEmail failed for {email}: "
            f"{data.get('error')}",
            flush=True,
        )
        return None
    return data["user"]["id"]


def _slack_open_dm(token: str, user_id: str) -> str | None:
    """Open a 1:1 DM with the user and return the D... channel ID.

    files.completeUploadExternal requires a real channel ID, unlike
    chat.postMessage which auto-resolves a U... user ID to a DM.
    """
    r = requests.post(
        "https://slack.com/api/conversations.open",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={"users": user_id},
        timeout=15,
    ).json()
    if not r.get("ok"):
        print(
            f"[rally.rollup] conversations.open failed for {user_id}: "
            f"{r.get('error')}",
            flush=True,
        )
        return None
    return r["channel"]["id"]


def _slack_upload_files(
    token: str,
    channel_id: str,
    files_to_upload: list[dict],
    initial_comment: str | None = None,
) -> bool:
    """Upload multiple files to a Slack DM/channel in a SINGLE message using
    the modern files.uploadV2 flow.

    files_to_upload entries: {"filename": str, "title": str, "bytes": bytes}

    Step 1 (per file): files.getUploadURLExternal  -> {upload_url, file_id}
    Step 2 (per file): POST bytes to upload_url
    Step 3 (once):     files.completeUploadExternal with files=[{id, title}, ...]
                       + channel_id (+ optional initial_comment) so all files
                       appear together in one DM message.
    """
    completed: list[dict] = []
    for spec in files_to_upload:
        # 1. Reserve an upload slot
        r1 = requests.get(
            "https://slack.com/api/files.getUploadURLExternal",
            headers={"Authorization": f"Bearer {token}"},
            params={"filename": spec["filename"], "length": len(spec["bytes"])},
            timeout=15,
        ).json()
        if not r1.get("ok"):
            print(
                f"[rally.rollup] getUploadURLExternal failed for "
                f"{spec['filename']}: {r1.get('error')}",
                flush=True,
            )
            return False

        # 2. Upload bytes
        r2 = requests.post(r1["upload_url"], data=spec["bytes"], timeout=60)
        if r2.status_code != 200:
            print(
                f"[rally.rollup] byte upload failed for {spec['filename']}: "
                f"HTTP {r2.status_code}",
                flush=True,
            )
            return False
        completed.append({"id": r1["file_id"], "title": spec["title"]})

    # 3. Complete: post all files in one DM message
    payload: dict = {"files": completed, "channel_id": channel_id}
    if initial_comment:
        payload["initial_comment"] = initial_comment
    r3 = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
        timeout=15,
    ).json()
    if not r3.get("ok"):
        print(
            f"[rally.rollup] completeUploadExternal failed: {r3.get('error')}",
            flush=True,
        )
        return False
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ws = WorkspaceClient()
    os.environ["PASE_TRACKER_PAT"] = _read_databricks_pat(ws)
    slack_token = _read_slack_token(ws)
    settings = get_settings()
    storage = MeetingStorage(settings)
    summarizer = MeetingSummarizer(settings)

    recipients: list[str] = load_recipients(storage)

    week_start, week_end = _current_work_week()
    print(
        f"[rally.rollup] window {week_start} -> {week_end}, "
        f"recipients={len(recipients)}",
        flush=True,
    )

    latest = storage.latest_in_range_per_pillar(week_start, week_end)
    pillar_summaries = _build_pillar_summaries(storage, latest)
    if not pillar_summaries:
        print(
            f"[rally.rollup] No submissions in {week_start}\u2013{week_end}; "
            f"nothing to generate.",
            flush=True,
        )
        return 0

    pillars_included = [get_pillar(s).name for s, r in latest.items() if r is not None]
    pillars_missing  = [get_pillar(s).name for s, r in latest.items() if r is None]
    print(
        f"[rally.rollup] included={pillars_included} missing={pillars_missing}",
        flush=True,
    )

    # ── Team rollup ──────────────────────────────────────────────────────────
    print("[rally.rollup] Generating Team Level rollup...", flush=True)
    team_extract = summarizer.generate_rollup(pillar_summaries)
    team_md, team_html = summarizer.render_rollup(team_extract)
    storage.save_rollup(
        title="Weekly Team Rollup",
        range_start=week_start,
        range_end=week_end,
        markdown=team_md,
        html=team_html,
        extract=team_extract,
        pillars_included=pillars_included,
        pillars_missing=pillars_missing,
        kind="team",
    )
    print("[rally.rollup] Team rollup saved to history.", flush=True)

    # ── Org rollup ───────────────────────────────────────────────────────────
    print("[rally.rollup] Generating Org Level rollup...", flush=True)
    org_extract = summarizer.generate_org_rollup(pillar_summaries)
    org_md, org_html = summarizer.render_org_rollup(org_extract)
    storage.save_rollup(
        title="Weekly Org Rollup",
        range_start=week_start,
        range_end=week_end,
        markdown=org_md,
        html=org_html,
        extract=org_extract,
        pillars_included=pillars_included,
        pillars_missing=pillars_missing,
        kind="org",
    )
    print("[rally.rollup] Org rollup saved to history.", flush=True)

    # ── Slack delivery ───────────────────────────────────────────────────────
    dm_text = _format_dm_text(week_start, week_end, team_md, org_md)
    week_tag = week_end.strftime("%Y-%m-%d")
    files_for_dm = [
        {
            "filename": f"rally_team_rollup_{week_tag}.md",
            "title":    f"Team Rollup (Markdown) \u2014 week of {week_end.strftime('%b %-d, %Y')}",
            "bytes":    team_md.encode("utf-8"),
        },
        {
            "filename": f"rally_team_rollup_{week_tag}.html",
            "title":    f"Team Rollup (HTML) \u2014 week of {week_end.strftime('%b %-d, %Y')}",
            "bytes":    team_html.encode("utf-8"),
        },
        {
            "filename": f"rally_org_rollup_{week_tag}.md",
            "title":    f"Org Rollup (Markdown) \u2014 week of {week_end.strftime('%b %-d, %Y')}",
            "bytes":    org_md.encode("utf-8"),
        },
        {
            "filename": f"rally_org_rollup_{week_tag}.html",
            "title":    f"Org Rollup (HTML) \u2014 week of {week_end.strftime('%b %-d, %Y')}",
            "bytes":    org_html.encode("utf-8"),
        },
    ]

    sent = skipped = failed = 0
    for email in recipients:
        email = (email or "").strip()
        if not email:
            skipped += 1
            continue
        user_id = _slack_lookup_user_id(slack_token, email)
        if not user_id:
            failed += 1
            continue
        channel_id = _slack_open_dm(slack_token, user_id)
        if not channel_id:
            failed += 1
            continue
        if _slack_upload_files(slack_token, channel_id, files_for_dm, initial_comment=dm_text):
            sent += 1
            print(f"[rally.rollup] DM sent to {email}", flush=True)
        else:
            failed += 1

    print(
        f"[rally.rollup] Done. sent={sent} skipped={skipped} failed={failed}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    # Note: do NOT wrap in sys.exit(). The Databricks serverless
    # spark_python_task runs this file inside an IPython kernel where
    # SystemExit is treated as a kernel abort and marks the run FAILED
    # even when the script completed successfully.
    main()
