"""Canonical Pacific-time helpers.

Storage convention: `created_at` / `updated_at` timestamps are always
written as UTC ISO 8601 (`datetime.now(timezone.utc).isoformat()`).
Anything a user perceives as "today" or "the date on this record" should
be derived in Pacific time instead, since that's where Rally's users are.
Use these helpers rather than `date.today()` or ad-hoc UTC formatting so
the whole app agrees on one conversion path.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_today() -> date:
    """Current calendar date in Pacific time. Use for any 'today' derivation
    that a user would perceive (default date ranges, last_mentioned stamps,
    week-boundary math when the calling code doesn't already have a full
    timestamp)."""
    return datetime.now(timezone.utc).astimezone(PACIFIC).date()


def pacific_now_display() -> str:
    """Format 'now' as 'Month Day, Year' in Pacific for user-visible footers
    (e.g. one-pager 'Generated ...' line)."""
    return datetime.now(timezone.utc).astimezone(PACIFIC).strftime("%B %-d, %Y")


def to_pacific_date(iso_ts: str | None) -> date | None:
    """Parse a stored UTC ISO timestamp (or a bare 'YYYY-MM-DD') and return
    its Pacific calendar date. Bare YYYY-MM-DD is returned as-is because
    those come from the LLM extract's meeting_date field and are already
    'the day the meeting happened' with no time component to convert."""
    if not iso_ts:
        return None
    try:
        if "T" not in iso_ts:
            return date.fromisoformat(iso_ts[:10])
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PACIFIC).date()
    except (ValueError, TypeError):
        return None
