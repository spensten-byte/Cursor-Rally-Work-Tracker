"""Persist uploads, generated summaries, and project registries per pillar.

Layout (per pillar slug):
  {root}/uploads/{pillar}/...
  {root}/summaries/{pillar}/...
  {root}/registries/project_registry_{pillar}.json

Backward-compat: legacy summaries at {root}/summaries/*.meta.json and the legacy
registry at {root}/project_registry.json are surfaced under the default pillar
(process-intelligence).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from zoneinfo import ZoneInfo

from config import Settings, get_settings
from pillars import DEFAULT_PILLAR_SLUG, all_slugs, get_pillar

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _week_anchor(now_utc: datetime | None = None) -> datetime:
    """Return the most recent Saturday 9 AM Pacific that is <= now.

    The work week runs Saturday 9:00 AM PT through the following Saturday 8:59:59 AM PT.
    Returned as an aware datetime in UTC.
    """
    now_pt = (now_utc or datetime.now(timezone.utc)).astimezone(_PACIFIC)
    # weekday(): Monday=0, Saturday=5, Sunday=6
    days_back = (now_pt.weekday() - 5) % 7
    candidate = (now_pt - timedelta(days=days_back)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    if candidate > now_pt:
        candidate -= timedelta(days=7)
    return candidate.astimezone(timezone.utc)


@dataclass
class NoteEntry:
    id: str
    author: str
    text: str
    pillar: str
    created_at: str
    week_key: str


@dataclass
class SummaryRecord:
    id: str
    meeting_title: str
    meeting_date: str
    created_at: str
    markdown_path: str
    html_path: str
    upload_path: str | None
    extract_path: str | None
    pillar: str = DEFAULT_PILLAR_SLUG


@dataclass
class RollupRecord:
    id: str
    title: str
    range_start: str
    range_end: str
    created_at: str
    markdown_path: str
    html_path: str
    extract_path: str
    pillars_included: list[str]
    pillars_missing: list[str]
    kind: str = "team"


class MeetingStorage:
    UPLOADS = "uploads"
    SUMMARIES = "summaries"
    REGISTRIES = "registries"
    ROLLUPS = "rollups"
    LEGACY_REGISTRY_FILE = "project_registry.json"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._use_volume = self._volume_available()

    # ------------------------------------------------------------------
    # Auth + backend probing
    # ------------------------------------------------------------------

    def _sdk_client(self):
        """Return a WorkspaceClient with an unambiguous auth method.

        The Databricks App platform always injects DATABRICKS_CLIENT_ID and
        DATABRICKS_CLIENT_SECRET (OAuth M2M) alongside DATABRICKS_TOKEN (empty
        string). Passing both to the SDK causes a 'more than one authorization
        method' error. We resolve this by explicitly specifying auth_type so
        the SDK ignores conflicting env-var credentials.
        """
        from databricks.sdk import WorkspaceClient

        if self.settings.databricks_token:
            return WorkspaceClient(
                host=self.settings.databricks_host,
                token=self.settings.databricks_token,
                auth_type="pat",
            )
        # No PAT — use the injected OAuth M2M credentials explicitly.
        import os
        client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
        client_secret = os.getenv("DATABRICKS_CLIENT_SECRET", "")
        if client_id and client_secret:
            return WorkspaceClient(
                host=self.settings.databricks_host,
                client_id=client_id,
                client_secret=client_secret,
                auth_type="oauth-m2m",
            )
        return WorkspaceClient()

    def _volume_available(self) -> bool:
        if not self.settings.databricks_host:
            return False
        if not self.settings.volume_path.startswith("/Volumes/"):
            return False
        # Use the SDK for the probe so auth (PAT or OAuth M2M) is handled
        # consistently across local dev, CI, and Databricks App environments.
        # list_directory_contents returns an iterator; calling next() or catching
        # StopIteration both confirm the path is accessible (empty dirs are fine).
        import urllib.request
        import urllib.error

        path = self.settings.volume_path.lstrip("/")
        url = f"{self.settings.databricks_host}/api/2.0/fs/directories/{path}"

        token = self.settings.databricks_token

        # Always try the SDK first — it resolves auth correctly in all contexts.
        try:
            list(self._sdk_client().files.list_directory_contents(self.settings.volume_path))
            return True
        except Exception:
            pass

        # SDK fallback: raw HTTP probe (works when urllib can reach the workspace).
        if not token:
            return False

        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as exc:
            return exc.code == 200
        except Exception:
            return False

    @property
    def backend(self) -> str:
        return "uc_volume" if self._use_volume else "local"

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _local_root(self) -> Path:
        root = self.settings.local_dir
        root.mkdir(parents=True, exist_ok=True)
        (root / self.UPLOADS).mkdir(exist_ok=True)
        (root / self.SUMMARIES).mkdir(exist_ok=True)
        (root / self.REGISTRIES).mkdir(exist_ok=True)
        return root

    def _slug(self, title: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
        return slug[:60] or "meeting"

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    def _normalize_pillar(self, pillar: str | None) -> str:
        return pillar if pillar in all_slugs() else DEFAULT_PILLAR_SLUG

    def _uploads_prefix(self, pillar: str) -> str:
        p = self._normalize_pillar(pillar)
        if self._use_volume:
            return f"{self.settings.volume_path}/{self.UPLOADS}/{p}"
        return str(self._local_root() / self.UPLOADS / p)

    def _summaries_prefix(self, pillar: str) -> str:
        p = self._normalize_pillar(pillar)
        if self._use_volume:
            return f"{self.settings.volume_path}/{self.SUMMARIES}/{p}"
        return str(self._local_root() / self.SUMMARIES / p)

    def _registry_path(self, pillar: str) -> str:
        p = self._normalize_pillar(pillar)
        if self._use_volume:
            return f"{self.settings.volume_path}/{self.REGISTRIES}/project_registry_{p}.json"
        return str(self._local_root() / self.REGISTRIES / f"project_registry_{p}.json")

    def _notes_prefix(self, pillar: str) -> str:
        p = self._normalize_pillar(pillar)
        if self._use_volume:
            return f"{self.settings.volume_path}/notes/{p}"
        return str(self._local_root() / "notes" / p)

    def _legacy_summaries_prefix(self) -> str:
        """Flat-layout summaries from before pillar restructuring."""
        if self._use_volume:
            return f"{self.settings.volume_path}/{self.SUMMARIES}"
        return str(self._local_root() / self.SUMMARIES)

    def _rollups_prefix(self) -> str:
        if self._use_volume:
            return f"{self.settings.volume_path}/{self.ROLLUPS}"
        return str(self._local_root() / self.ROLLUPS)

    def _legacy_registry_path(self) -> str:
        if self._use_volume:
            return f"{self.settings.volume_path}/{self.LEGACY_REGISTRY_FILE}"
        return str(self._local_root() / self.LEGACY_REGISTRY_FILE)

    # ------------------------------------------------------------------
    # Raw bytes IO
    # ------------------------------------------------------------------

    def _delete_file(self, path: str) -> None:
        """Delete a single file, silently ignoring missing-file errors."""
        try:
            if self._use_volume and path.startswith("/Volumes/"):
                self._sdk_client().files.delete(path)
            else:
                p = Path(path)
                if p.exists():
                    p.unlink()
        except Exception:
            pass

    def _write_bytes(self, path: str, data: bytes) -> None:
        if self._use_volume and path.startswith("/Volumes/"):
            self._sdk_client().files.upload(path, BytesIO(data), overwrite=True)
        else:
            dest = Path(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

    def _read_bytes(self, path: str) -> bytes:
        if self._use_volume and path.startswith("/Volumes/"):
            response = self._sdk_client().files.download(path)
            return response.contents.read()
        return Path(path).read_bytes()

    def _list_meta_files(self, prefix: str) -> list[str]:
        """Return absolute paths of `*.meta.json` files under `prefix` (newest first)."""
        if self._use_volume and prefix.startswith("/Volumes/"):
            try:
                listing = self._sdk_client().files.list_directory_contents(prefix + "/")
                return sorted(
                    [e.path for e in listing if e.path.endswith(".meta.json")],
                    reverse=True,
                )
            except Exception:
                return []
        directory = Path(prefix)
        if not directory.exists():
            return []
        return sorted(
            [str(p) for p in directory.glob("*.meta.json")],
            reverse=True,
        )

    # ------------------------------------------------------------------
    # Uploads
    # ------------------------------------------------------------------

    def save_upload(self, filename: str, content: bytes, pillar: str = DEFAULT_PILLAR_SLUG) -> str:
        stamp = self._stamp()
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename)
        prefix = self._uploads_prefix(pillar)
        path = f"{prefix}/{stamp}_{safe_name}" if self._use_volume else str(Path(prefix) / f"{stamp}_{safe_name}")
        self._write_bytes(path, content)
        return path

    # ------------------------------------------------------------------
    # Collaborative notes
    # ------------------------------------------------------------------

    @staticmethod
    def _current_week_key() -> str:
        """Return the Sat-9am-PT week anchor as 'YYYY-MM-DD', e.g. '2026-06-07'."""
        return _week_anchor().astimezone(_PACIFIC).strftime("%Y-%m-%d")

    def save_note(self, author: str, text: str, pillar: str = DEFAULT_PILLAR_SLUG) -> NoteEntry:
        pillar = self._normalize_pillar(pillar)
        stamp = self._stamp()
        safe_author = re.sub(r"[^a-zA-Z0-9]+", "-", author.split("@")[0].lower())[:20]
        note_id = f"{stamp}_{safe_author}"
        week_key = self._current_week_key()
        entry = {
            "id": note_id,
            "author": author,
            "text": text,
            "pillar": pillar,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "week_key": week_key,
        }
        prefix = self._notes_prefix(pillar)
        path = (
            f"{prefix}/{note_id}.note.json"
            if self._use_volume
            else str(Path(prefix) / f"{note_id}.note.json")
        )
        self._write_bytes(path, json.dumps(entry, indent=2).encode("utf-8"))
        return NoteEntry(**entry)

    def _list_note_files(self, prefix: str) -> list[str]:
        if self._use_volume and prefix.startswith("/Volumes/"):
            try:
                listing = self._sdk_client().files.list_directory_contents(prefix + "/")
                return sorted(
                    [e.path for e in listing if e.path.endswith(".note.json")],
                    reverse=True,
                )
            except Exception:
                return []
        directory = Path(prefix)
        if not directory.exists():
            return []
        return sorted(
            [str(p) for p in directory.glob("*.note.json")],
            reverse=True,
        )

    def list_notes(self, pillar: str, week_key: str | None = None) -> list[NoteEntry]:
        prefix = self._notes_prefix(pillar)
        entries: list[NoteEntry] = []
        for path in self._list_note_files(prefix):
            try:
                raw = self._read_bytes(path)
                data = json.loads(raw.decode("utf-8"))
                entry = NoteEntry(
                    id=data["id"],
                    author=data.get("author", "unknown"),
                    text=data.get("text", ""),
                    pillar=data.get("pillar", pillar),
                    created_at=data.get("created_at", ""),
                    week_key=data.get("week_key", ""),
                )
                if week_key is None or entry.week_key == week_key:
                    entries.append(entry)
            except Exception:
                continue
        return entries

    def delete_note(self, note_id: str, pillar: str = DEFAULT_PILLAR_SLUG) -> None:
        prefix = self._notes_prefix(pillar)
        path = (
            f"{prefix}/{note_id}.note.json"
            if self._use_volume
            else str(Path(prefix) / f"{note_id}.note.json")
        )
        self._delete_file(path)

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------

    def save_summary(
        self,
        meeting_title: str,
        meeting_date: str,
        markdown: str,
        html: str,
        extract: dict,
        upload_path: str | None,
        source_filename: str | None,
        pillar: str = DEFAULT_PILLAR_SLUG,
    ) -> SummaryRecord:
        pillar = self._normalize_pillar(pillar)
        stamp = self._stamp()
        slug = self._slug(meeting_title)
        base = f"{stamp}_{slug}"
        record_id = base

        meta = {
            "id": record_id,
            "meeting_title": meeting_title,
            "meeting_date": meeting_date,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_filename": source_filename,
            "upload_path": upload_path,
            "pillar": pillar,
        }

        prefix = self._summaries_prefix(pillar)
        joiner = "/" if self._use_volume else None

        def _join(*parts: str) -> str:
            if self._use_volume:
                return joiner.join(parts)
            return str(Path(*parts))

        md_path = _join(prefix, f"{base}.md")
        html_path = _join(prefix, f"{base}.html")
        extract_path = _join(prefix, f"{base}.extract.json")
        meta_path = _join(prefix, f"{base}.meta.json")

        self._write_bytes(md_path, markdown.encode("utf-8"))
        self._write_bytes(html_path, html.encode("utf-8"))
        self._write_bytes(extract_path, json.dumps(extract, indent=2).encode("utf-8"))
        meta["markdown_path"] = md_path
        meta["html_path"] = html_path
        meta["extract_path"] = extract_path
        self._write_bytes(meta_path, json.dumps(meta, indent=2).encode("utf-8"))

        return SummaryRecord(
            id=record_id,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            created_at=meta["created_at"],
            markdown_path=meta["markdown_path"],
            html_path=meta["html_path"],
            upload_path=upload_path,
            extract_path=meta.get("extract_path"),
            pillar=pillar,
        )

    def _record_from_meta_path(self, meta_path: str) -> SummaryRecord | None:
        try:
            raw = self._read_bytes(meta_path)
            meta = json.loads(raw.decode("utf-8"))
            return SummaryRecord(
                id=meta["id"],
                meeting_title=meta.get("meeting_title", ""),
                meeting_date=meta.get("meeting_date", ""),
                created_at=meta.get("created_at", ""),
                markdown_path=meta.get("markdown_path", ""),
                html_path=meta.get("html_path", ""),
                upload_path=meta.get("upload_path"),
                extract_path=meta.get("extract_path"),
                pillar=meta.get("pillar", DEFAULT_PILLAR_SLUG),
            )
        except Exception:
            return None

    def list_summaries(
        self,
        limit: int = 50,
        pillar: str | None = None,
    ) -> list[SummaryRecord]:
        """List archived summaries for one pillar (or all pillars if `pillar` is None).

        When `pillar` is the default pillar, legacy flat-layout summaries are also
        included so existing data stays accessible after the pillar restructure.
        """
        records: list[SummaryRecord] = []

        if pillar is None:
            targets = all_slugs()
        else:
            targets = [self._normalize_pillar(pillar)]

        for target in targets:
            for meta_path in self._list_meta_files(self._summaries_prefix(target)):
                record = self._record_from_meta_path(meta_path)
                if record is not None:
                    if record.pillar != target:
                        record.pillar = target
                    records.append(record)

        # Backward-compat: legacy flat-layout summaries surface under the default pillar
        # (or in the "all pillars" view).
        if pillar in (None, DEFAULT_PILLAR_SLUG):
            for meta_path in self._list_meta_files(self._legacy_summaries_prefix()):
                # Skip files that live inside per-pillar subfolders (already counted).
                tail = meta_path.replace("\\", "/").split("/summaries/", 1)[-1]
                if "/" in tail:
                    continue
                record = self._record_from_meta_path(meta_path)
                if record is not None:
                    record.pillar = DEFAULT_PILLAR_SLUG
                    records.append(record)

        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def load_recent_context(
        self,
        count: int | None = None,
        pillar: str = DEFAULT_PILLAR_SLUG,
    ) -> str:
        n = count if count is not None else self.settings.context_summary_count
        blocks: list[str] = []
        for record in self.list_summaries(limit=n, pillar=pillar):
            try:
                md = self._read_bytes(record.markdown_path).decode("utf-8")
                blocks.append(
                    f"--- Prior summary: {record.meeting_title} ({record.meeting_date}) ---\n{md}"
                )
            except Exception:
                continue
        return "\n\n".join(blocks)

    def read_file(self, path: str) -> str:
        return self._read_bytes(path).decode("utf-8")

    # ------------------------------------------------------------------
    # Project Registry — persistent cross-week project state, per pillar
    # ------------------------------------------------------------------

    def load_registry(self, pillar: str = DEFAULT_PILLAR_SLUG) -> dict:
        """Return the project registry for `pillar`, falling back to legacy registry
        only for the default pillar."""
        pillar = self._normalize_pillar(pillar)
        try:
            raw = self._read_bytes(self._registry_path(pillar))
            return json.loads(raw.decode("utf-8"))
        except Exception:
            pass

        if pillar == DEFAULT_PILLAR_SLUG:
            try:
                raw = self._read_bytes(self._legacy_registry_path())
                return json.loads(raw.decode("utf-8"))
            except Exception:
                pass

        return {"last_updated": "", "projects": []}

    def save_registry(self, registry: dict, pillar: str = DEFAULT_PILLAR_SLUG) -> None:
        pillar = self._normalize_pillar(pillar)
        registry["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._write_bytes(
            self._registry_path(pillar),
            json.dumps(registry, indent=2).encode("utf-8"),
        )

    def update_registry_from_extract(
        self,
        extract: dict,
        pillar: str = DEFAULT_PILLAR_SLUG,
    ) -> dict:
        """Merge an extract's team_members[].projects[] into the pillar registry."""
        status_map = {
            "in_progress": "In Progress",
            "blocked": "Blocked",
            "complete": "Complete",
            "paused": "Paused",
        }
        registry = self.load_registry(pillar)
        projects: list[dict] = registry.get("projects", [])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def _find(name: str, project: str) -> dict | None:
            for p in projects:
                if (
                    p.get("name", "").lower() == name.lower()
                    and p.get("project", "").lower() == project.lower()
                ):
                    return p
            return None

        for member in extract.get("team_members", []):
            owner = member.get("name", "") or member.get("short_name", "")
            if not owner:
                continue
            for proj in member.get("projects", []):
                project_name = proj.get("name", "")
                if not project_name:
                    continue
                raw_status = (proj.get("status") or "").lower()
                mapped_status = status_map.get(raw_status, "In Progress")
                existing = _find(owner, project_name)
                if existing:
                    existing["status"] = mapped_status
                    existing["loe"] = proj.get("loe", existing.get("loe", ""))
                    existing["target"] = proj.get("target", existing.get("target", ""))
                    existing["dependencies"] = proj.get(
                        "dependencies", existing.get("dependencies", "")
                    )
                    existing["notes"] = proj.get("detail", existing.get("notes", ""))
                    existing["last_mentioned"] = today
                else:
                    projects.append({
                        "name": owner,
                        "project": project_name,
                        "workstream": "",
                        "loe": proj.get("loe", ""),
                        "hrs_wk": "",
                        "status": mapped_status,
                        "target": proj.get("target", ""),
                        "dependencies": proj.get("dependencies", ""),
                        "notes": proj.get("detail", ""),
                        "added_date": today,
                        "last_mentioned": today,
                    })

        registry["projects"] = projects
        self.save_registry(registry, pillar)
        return registry

    def registry_as_context(self, pillar: str = DEFAULT_PILLAR_SLUG) -> str:
        """Return active registry projects as a formatted string for LLM context."""
        registry = self.load_registry(pillar)
        projects = [p for p in registry.get("projects", []) if p.get("status") not in ("Complete",)]
        if not projects:
            return ""
        lines = ["## Active project registry (carry forward — do not drop items not mentioned this week)\n"]
        by_person: dict[str, list] = {}
        for p in projects:
            by_person.setdefault(p.get("name", "Unassigned"), []).append(p)
        for person, items in sorted(by_person.items()):
            lines.append(f"### {person}")
            for item in items:
                status = item.get("status", "In Progress")
                loe = item.get("loe", "")
                last = item.get("last_mentioned", "")
                lines.append(f"- [{status}] {item.get('project', '')} (LOE: {loe}, last mentioned: {last})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Rollup helpers
    # ------------------------------------------------------------------

    def list_summaries_for_rollup(self, limit: int = 20) -> list[SummaryRecord]:
        """Return recent summaries across all pillars (used by older callers)."""
        return self.list_summaries(limit=limit, pillar=None)

    # ------------------------------------------------------------------
    # Leadership rollup archive
    # ------------------------------------------------------------------

    def save_rollup(
        self,
        title: str,
        range_start: date,
        range_end: date,
        markdown: str,
        html: str,
        extract: dict,
        pillars_included: list[str],
        pillars_missing: list[str],
        kind: str = "team",
    ) -> RollupRecord:
        stamp = self._stamp()
        slug = self._slug(title)
        base = f"{stamp}_{slug}"
        prefix = self._rollups_prefix()

        def _join(*parts: str) -> str:
            if self._use_volume:
                return "/".join(parts)
            return str(Path(*parts))

        md_path = _join(prefix, f"{base}.md")
        html_path = _join(prefix, f"{base}.html")
        extract_path = _join(prefix, f"{base}.extract.json")
        meta_path = _join(prefix, f"{base}.meta.json")

        self._write_bytes(md_path, markdown.encode("utf-8"))
        self._write_bytes(html_path, html.encode("utf-8"))
        self._write_bytes(extract_path, json.dumps(extract, indent=2).encode("utf-8"))

        meta = {
            "id": base,
            "title": title,
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "markdown_path": md_path,
            "html_path": html_path,
            "extract_path": extract_path,
            "pillars_included": pillars_included,
            "pillars_missing": pillars_missing,
            "kind": kind,
        }
        self._write_bytes(meta_path, json.dumps(meta, indent=2).encode("utf-8"))

        return RollupRecord(
            id=base,
            title=title,
            range_start=meta["range_start"],
            range_end=meta["range_end"],
            created_at=meta["created_at"],
            markdown_path=md_path,
            html_path=html_path,
            extract_path=extract_path,
            pillars_included=pillars_included,
            pillars_missing=pillars_missing,
            kind=kind,
        )

    def list_rollups(self, limit: int = 30, kind: str | None = None) -> list[RollupRecord]:
        """Return saved leadership rollups, newest first.

        Pass ``kind="team"`` or ``kind="org"`` to filter by type.
        Existing records without a ``kind`` field are treated as ``"team"``.
        """
        prefix = self._rollups_prefix()
        records: list[RollupRecord] = []
        try:
            meta_paths = sorted(
                self._list_meta_files(prefix), reverse=True
            )
        except Exception:
            return []
        for meta_path in meta_paths:
            if len(records) >= limit:
                break
            try:
                raw = self._read_bytes(meta_path)
                meta = json.loads(raw.decode("utf-8"))
                rec_kind = meta.get("kind", "team")
                if kind is not None and rec_kind != kind:
                    continue
                records.append(
                    RollupRecord(
                        id=meta["id"],
                        title=meta.get("title", ""),
                        range_start=meta.get("range_start", ""),
                        range_end=meta.get("range_end", ""),
                        created_at=meta.get("created_at", ""),
                        markdown_path=meta.get("markdown_path", ""),
                        html_path=meta.get("html_path", ""),
                        extract_path=meta.get("extract_path", ""),
                        pillars_included=meta.get("pillars_included", []),
                        pillars_missing=meta.get("pillars_missing", []),
                        kind=rec_kind,
                    )
                )
            except Exception:
                continue
        return records

    def delete_rollup(self, record: RollupRecord) -> None:
        prefix = self._rollups_prefix()
        base = record.id

        def _join(*parts: str) -> str:
            if self._use_volume:
                return "/".join(parts)
            return str(Path(*parts))

        for path in [
            _join(prefix, f"{base}.md"),
            _join(prefix, f"{base}.html"),
            _join(prefix, f"{base}.extract.json"),
            _join(prefix, f"{base}.meta.json"),
        ]:
            self._delete_file(path)

    def delete_summary(self, record: SummaryRecord) -> None:
        """Delete all files associated with a summary record.

        Removes the markdown, HTML, extract JSON, meta JSON, and uploaded
        source file. Missing files are silently ignored.
        """
        prefix = self._summaries_prefix(record.pillar)
        base = record.id
        joiner = "/" if self._use_volume else None

        def _join(*parts: str) -> str:
            if self._use_volume:
                return "/".join(parts)
            return str(Path(*parts))

        for path in [
            _join(prefix, f"{base}.md"),
            _join(prefix, f"{base}.html"),
            _join(prefix, f"{base}.extract.json"),
            _join(prefix, f"{base}.meta.json"),
        ]:
            self._delete_file(path)

        if record.upload_path:
            self._delete_file(record.upload_path)

    def load_all_history_context(self) -> tuple[str, int]:
        """Return concatenated markdown for every saved summary across all pillars
        (newest first per pillar) and the total count of summaries included."""
        blocks: list[str] = []
        count = 0
        for slug in all_slugs():
            pillar_name = get_pillar(slug).name
            for record in self.list_summaries(limit=200, pillar=slug):
                try:
                    md = self._read_bytes(record.markdown_path).decode("utf-8")
                    eff = self._record_date(record)
                    date_str = eff.isoformat() if eff else (record.meeting_date or "no-date")
                    blocks.append(
                        f"=== {pillar_name} | One-pager | {record.meeting_title} | {date_str} ===\n{md}"
                    )
                    count += 1
                except Exception:
                    continue
            notes = self.list_notes(slug)
            if notes:
                by_week: dict[str, list[NoteEntry]] = {}
                for n in notes:
                    by_week.setdefault(n.week_key or "no-week", []).append(n)
                for wk in sorted(by_week.keys(), reverse=True):
                    lines = [f"=== {pillar_name} | Saved notes | Week of {wk} ==="]
                    for n in by_week[wk]:
                        lines.append(f"[{n.author} · {n.created_at}]\n{n.text}")
                    blocks.append("\n\n".join(lines))
                    count += 1
        return "\n\n".join(blocks), count

    def list_latest_per_pillar(self) -> dict[str, SummaryRecord | None]:
        """Return the most recent summary for each pillar slug (None if missing)."""
        latest: dict[str, SummaryRecord | None] = {slug: None for slug in all_slugs()}
        for slug in all_slugs():
            records = self.list_summaries(limit=1, pillar=slug)
            latest[slug] = records[0] if records else None
        return latest

    # ------------------------------------------------------------------
    # Date-range helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _record_date(record: SummaryRecord) -> date | None:
        """Return the effective date of a record for filtering purposes.

        Prefers `created_at` (when the submission was generated) so that the
        Leadership Rollup date range filters on submission date rather than
        meeting date. This ensures recently submitted notes always surface in
        the rollup regardless of when the underlying meeting occurred.
        Falls back to `meeting_date` if `created_at` is unavailable.
        """
        for raw in (record.created_at, record.meeting_date):
            if raw:
                try:
                    return date.fromisoformat(raw[:10])
                except ValueError:
                    continue
        return None

    def list_in_range(
        self,
        pillar: str,
        start: date,
        end: date,
    ) -> list[SummaryRecord]:
        """Return summaries for `pillar` whose effective date falls within [start, end]."""
        return [
            r for r in self.list_summaries(limit=200, pillar=pillar)
            if (d := self._record_date(r)) is not None and start <= d <= end
        ]

    def latest_in_range_per_pillar(
        self,
        start: date,
        end: date,
    ) -> dict[str, SummaryRecord | None]:
        """Return the most recent in-range summary per pillar (None when missing)."""
        result: dict[str, SummaryRecord | None] = {}
        for slug in all_slugs():
            in_range = self.list_in_range(slug, start, end)
            result[slug] = in_range[0] if in_range else None
        return result


def read_uploaded_text(filename: str, data: BinaryIO | bytes) -> str:
    name = filename.lower()
    raw = data if isinstance(data, bytes) else data.read()

    if name.endswith(".docx"):
        try:
            from docx import Document

            doc = Document(BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError as exc:
            raise RuntimeError("python-docx is required for .docx uploads") from exc

    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(raw))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(p.strip() for p in pages if p.strip())
            if not text:
                raise ValueError("No readable text found in PDF. Try copying the text and pasting it instead.")
            return text
        except ImportError as exc:
            raise RuntimeError("pypdf is required for .pdf uploads") from exc

    if name.endswith((".xlsx", ".xls")):
        try:
            import openpyxl

            wb = openpyxl.load_workbook(BytesIO(raw), read_only=True, data_only=True)
            sections: list[str] = []
            for sheet in wb.worksheets:
                rows: list[str] = []
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                    if cells:
                        rows.append("\t".join(cells))
                if rows:
                    sections.append(f"[Sheet: {sheet.title}]\n" + "\n".join(rows))
            wb.close()
            if not sections:
                raise ValueError("No readable content found in Excel file.")
            return "\n\n".join(sections)
        except ImportError as exc:
            raise RuntimeError("openpyxl is required for .xlsx/.xls uploads") from exc

    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode file: {filename}")
