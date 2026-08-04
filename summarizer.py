"""Two-step pipeline: LLM extract → template render."""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import PACKAGE_DIR, Settings, get_settings
from pillars import PILLARS
from time_utils import pacific_now_display

EXTRACT_PROMPT_PATH = PACKAGE_DIR / "prompts" / "extract.md"
ROLLUP_PROMPT_PATH = PACKAGE_DIR / "prompts" / "rollup.md"
TEMPLATE_DIR = PACKAGE_DIR / "templates"

# Prompts that the settings popover can view/edit.
PROMPT_NAMES = ("extract", "rollup", "exec_rollup")

# Appended at runtime to the active exec_rollup prompt so pillar source
# breadcrumbs survive even if a user has edited/overridden the base prompt.
ORG_SOURCE_BREADCRUMB_ADDENDUM = """Source breadcrumb requirement:
For every item in `top_priorities`, `critical_issues`, and `decisions_needed`, end the bullet with a subtle source breadcrumb in square brackets, using the pillar name or names that materially contributed to that work.

Examples:
- "Returns audit remediation — reframe flagged-amount logic and SEC trace cleanup. [Process Enablement]"
- "EDI incident redesign — align process map and intake flow across teams. [Distribution Excellence, Process Intelligence]"

Use only pillar names present in the input headings. If a bullet truly synthesizes all submitted pillars, use `[Multiple pillars]`. Do not create a separate source section and do not turn the report into a per-pillar breakdown."""


@contextlib.contextmanager
def _isolated_databricks_auth(*, keep: str) -> Iterator[None]:
    """Temporarily clear conflicting Databricks auth env vars so the SDK
    Config validator sees exactly one authentication method.

    Parameters
    ----------
    keep : str
        Which auth flow to keep visible to the SDK during construction.
        One of: "pat", "oauth-m2m".
    """
    if keep == "pat":
        strip: tuple[str, ...] = ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET")
    elif keep == "oauth-m2m":
        strip = ("DATABRICKS_TOKEN",)
    else:
        strip = ()
    saved = {k: os.environ.pop(k, None) for k in strip}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _format_date(value: str) -> str:
    """Convert YYYY-MM-DD to 'May 26, 2026'; leave other strings unchanged."""
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d")
        return dt.strftime("%B %-d, %Y") if hasattr(dt, "strftime") else value
    except (ValueError, AttributeError):
        return value


REQUIRED_KEYS = {
    "decisions",
    "closures",
    "new_tracks",
    "reassignments",
    "watch_list",
    "gaps",
    "next_steps",
}


def _normalize_pillar_key(s: str) -> str:
    """Lowercase, strip, and treat '&' as equivalent to 'and' for pillar matching."""
    return re.sub(r"\s*&\s*", " and ", s.strip().lower())


def _strip_excellence_suffix(s: str) -> str:
    """Drop a trailing 'excellence' word (LLMs often shorten pillar names this way)."""
    return re.sub(r"\s+excellence\s*$", "", s).strip()


def _canonicalize_pillar_name(raw: str) -> str:
    """Map any pillar string (case/whitespace/short form, & or 'and', with or
    without trailing 'Excellence') to its canonical Pillar.name.

    Returns the raw string stripped if no canonical match (preserves custom labels).
    """
    if not raw:
        return raw
    key = _normalize_pillar_key(raw)
    key_no_exc = _strip_excellence_suffix(key)
    for p in PILLARS:
        norm_name = _normalize_pillar_key(p.name)
        norm_short = _normalize_pillar_key(p.short)
        candidates = {
            norm_name,
            norm_short,
            _strip_excellence_suffix(norm_name),
            _strip_excellence_suffix(norm_short),
            p.slug.lower(),
        }
        candidates.discard("")
        if key in candidates or key_no_exc in candidates:
            return p.name
    return raw.strip()


@dataclass
class OnePagerResult:
    extract: dict[str, Any]
    markdown: str
    html: str


class MeetingSummarizer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._jinja = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self._jinja.filters["format_date"] = _format_date

    # ------------------------------------------------------------------
    # Prompt loading (with UC Volume override + repo fallback)
    # ------------------------------------------------------------------

    def _use_volume(self) -> bool:
        return bool(self.settings.databricks_host) and self.settings.volume_path.startswith("/Volumes/")

    def _volume_sdk_client(self):
        from databricks.sdk import WorkspaceClient
        if self.settings.databricks_token:
            with _isolated_databricks_auth(keep="pat"):
                return WorkspaceClient(
                    host=self.settings.databricks_host,
                    token=self.settings.databricks_token,
                    auth_type="pat",
                )
        client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
        client_secret = os.getenv("DATABRICKS_CLIENT_SECRET", "")
        if client_id and client_secret:
            with _isolated_databricks_auth(keep="oauth-m2m"):
                return WorkspaceClient(
                    host=self.settings.databricks_host,
                    client_id=client_id,
                    client_secret=client_secret,
                    auth_type="oauth-m2m",
                )
        return WorkspaceClient()

    def _override_path(self, name: str) -> str:
        return f"{self.settings.volume_path}/prompts_overrides/{name}.md"

    def _meta_path(self, name: str) -> str:
        return f"{self.settings.volume_path}/prompts_overrides/{name}.meta.json"

    def _default_prompt_path(self, name: str) -> "Path":
        from pathlib import Path
        return PACKAGE_DIR / "prompts" / f"{name}.md"

    def load_prompt(self, name: str) -> str:
        """Return the active prompt text, preferring UC Volume override."""
        if name not in PROMPT_NAMES:
            raise ValueError(f"Unknown prompt: {name}")
        if self._use_volume():
            try:
                response = self._volume_sdk_client().files.download(self._override_path(name))
                return response.contents.read().decode("utf-8")
            except Exception:
                pass  # fall back to the repo default
        return self._default_prompt_path(name).read_text(encoding="utf-8")

    def save_prompt(self, name: str, content: str, edited_by: str) -> None:
        """Persist an override prompt + audit metadata to UC Volume."""
        if name not in PROMPT_NAMES:
            raise ValueError(f"Unknown prompt: {name}")
        if not content.strip():
            raise ValueError("Prompt body cannot be empty.")
        if not self._use_volume():
            raise RuntimeError(
                "Saving prompt overrides requires a UC Volume configuration."
            )
        from io import BytesIO
        client = self._volume_sdk_client()
        client.files.upload(
            self._override_path(name),
            BytesIO(content.encode("utf-8")),
            overwrite=True,
        )
        meta = {
            "updated_by": edited_by or "unknown",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        client.files.upload(
            self._meta_path(name),
            BytesIO(json.dumps(meta, indent=2).encode("utf-8")),
            overwrite=True,
        )

    def reset_prompt(self, name: str) -> None:
        """Remove the override + meta so the repo default takes over."""
        if name not in PROMPT_NAMES:
            raise ValueError(f"Unknown prompt: {name}")
        if not self._use_volume():
            return
        client = self._volume_sdk_client()
        for path in (self._override_path(name), self._meta_path(name)):
            try:
                client.files.delete(path)
            except Exception:
                pass

    def prompt_meta(self, name: str) -> dict[str, Any] | None:
        """Return the audit metadata for an override, or None if no override exists."""
        if name not in PROMPT_NAMES or not self._use_volume():
            return None
        try:
            response = self._volume_sdk_client().files.download(self._meta_path(name))
            return json.loads(response.contents.read().decode("utf-8"))
        except Exception:
            return None

    def _get_token(self) -> str:
        """Return a valid Databricks token for LLM API calls.

        Uses the explicit PAT when available. Falls back to the injected OAuth
        M2M credentials (DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET) that
        the Databricks App platform provides automatically.
        """
        if self.settings.databricks_token:
            return self.settings.databricks_token
        import os
        from databricks.sdk import WorkspaceClient
        client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
        client_secret = os.getenv("DATABRICKS_CLIENT_SECRET", "")
        if client_id and client_secret:
            return WorkspaceClient(
                host=self.settings.databricks_host,
                client_id=client_id,
                client_secret=client_secret,
                auth_type="oauth-m2m",
            ).config.token
        return WorkspaceClient().config.token

    def _chat(self, system: str, user: str) -> str:
        if self.settings.mock_llm:
            raise RuntimeError("mock_llm enabled — use summarize_from_extract instead")

        from openai import OpenAI

        client = OpenAI(
            api_key=self._get_token(),
            base_url=f"{self.settings.databricks_host}/serving-endpoints",
        )
        response = client.chat.completions.create(
            model=self.settings.model_endpoint,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=16384,
        )
        choice = response.choices[0]
        finish = getattr(choice, "finish_reason", None)
        usage = getattr(response, "usage", None)
        prompt_tok = getattr(usage, "prompt_tokens", None) if usage else None
        completion_tok = getattr(usage, "completion_tokens", None) if usage else None
        content = choice.message.content or ""
        print(
            f"[rally._chat] finish_reason={finish} "
            f"prompt_tokens={prompt_tok} completion_tokens={completion_tok} "
            f"content_chars={len(content)}",
            flush=True,
        )
        if finish == "length":
            raise ValueError(
                "The model hit its output token limit before finishing the response "
                f"(completion_tokens={completion_tok}). This usually means the transcript "
                "has many attendees with multiple projects each. Try trimming the notes "
                "to the most important sections and regenerating."
            )
        return content

    def _chat_and_parse_json(
        self,
        prompt_name: str,
        user_content: str,
        required_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """LLM JSON call with one auto-retry if model returns prose OR wrong shape.

        On the happy path this is a single LLM call (no overhead). Two recovery
        paths are handled:
        - Prose: the response has no `{` at all.
        - Schema drift: the response is valid JSON but none of the expected
          renderer keys are present (typical when a UC Volume override uses
          different field names).
        """
        system = self.load_prompt(prompt_name)
        raw = self._chat(system, user_content)

        if "{" not in raw:
            return self._retry_json(prompt_name, system, user_content, reason="prose")

        parsed = self._parse_json(raw)

        if required_keys and not self._any_required_key_present(parsed, required_keys):
            print(
                f"[rally.{prompt_name}] parsed JSON missing all required keys "
                f"{required_keys}; got keys {list(parsed.keys())}; retrying with "
                "schema-strict addendum",
                flush=True,
            )
            return self._retry_json(
                prompt_name, system, user_content,
                reason="schema", required_keys=required_keys,
            )

        return parsed

    @staticmethod
    def _any_required_key_present(parsed: dict[str, Any], keys: list[str]) -> bool:
        """Return True if at least one of `keys` has a non-empty value in `parsed`."""
        for k in keys:
            v = parsed.get(k)
            if isinstance(v, str) and v.strip():
                return True
            if isinstance(v, (list, dict)) and len(v) > 0:
                return True
        return False

    def _retry_json(
        self,
        prompt_name: str,
        base_system: str,
        user_content: str,
        *,
        reason: str,
        required_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Retry a failed JSON call with a correction addendum appended to the system prompt."""
        if reason == "prose":
            print(
                f"[rally.{prompt_name}] model returned prose (no '{{' found); "
                "retrying with stricter JSON-only system addendum",
                flush=True,
            )
            addendum = (
                "CRITICAL: Your previous response was prose, not JSON. "
                "Respond with ONLY a JSON object \u2014 first character `{`, last `}`."
            )
        else:
            keys = ", ".join(required_keys or [])
            addendum = (
                "CRITICAL: Your previous JSON used keys the renderer does not recognize. "
                f"You MUST use exactly these top-level keys: {keys}. "
                "Respond with ONLY a JSON object."
            )
        raw = self._chat(base_system + "\n\n" + addendum, user_content)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        # Strip BOM and zero-width spaces that survive a plain .strip()
        text = text.lstrip("\ufeff\u200b").strip()
        if not text:
            raise ValueError(
                "The model returned an empty response. This usually means the output was "
                "cut off by the token limit or the model refused the request. Try shorter notes."
            )
        original_text = text
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence:
            captured = fence.group(1).strip()
            # Only use the captured content if it's non-empty; otherwise fall
            # back to the full response (handles malformed/empty fence blocks).
            text = captured if captured else original_text
        else:
            # Unclosed fence: response was truncated before the closing ```.
            # Strip the opening fence so the brace-extraction below can work
            # and the truncation hint fires instead of a misleading "char 0".
            open_fence = re.match(r"^```(?:json)?\s*\n?", text)
            if open_fence:
                text = text[open_fence.end():].strip()
        # Handle prose preamble / reasoning blocks: extract substring between
        # the first { and last } before attempting a full parse.
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace : last_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass  # fall through to original-text parse for a clearer error
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            preview = original_text[:300] + ("…" if len(original_text) > 300 else "")
            has_opening_brace = "{" in original_text
            if not has_opening_brace:
                hint = (
                    "\n\nThe model returned narrative text instead of JSON. "
                    "Please click Generate again. If this keeps happening, an admin "
                    "should review the prompt for this rollup type."
                )
            elif not original_text.rstrip().endswith("}"):
                hint = (
                    "\n\nThe response appears to have been cut off mid-JSON. "
                    "This usually means the notes were too long for the model's output limit. "
                    "Try shorter notes or trim the transcript to the most important sections."
                )
            else:
                hint = ""
            raise ValueError(
                f"Could not parse model response as JSON ({exc}). Response preview:\n{preview}{hint}"
            ) from exc

    @staticmethod
    def _normalize_decisions(items: list) -> list[dict]:
        out = []
        for it in items or []:
            if isinstance(it, str):
                out.append({"decision": it, "context": ""})
            elif isinstance(it, dict):
                out.append({
                    "decision": it.get("decision", it.get("item", "")),
                    "context": it.get("context", ""),
                })
        return out

    @staticmethod
    def _normalize_with_summary(items: list, key: str = "item") -> list[dict]:
        out = []
        for it in items or []:
            if isinstance(it, str):
                out.append({key: it, "owner": "", "summary": ""})
            elif isinstance(it, dict):
                out.append({
                    key: it.get(key, ""),
                    "owner": it.get("owner", ""),
                    "summary": it.get("summary", ""),
                })
        return out

    @staticmethod
    def _normalize_risks(items: list) -> list[dict]:
        out = []
        for it in items or []:
            if isinstance(it, str):
                out.append({"risk": it, "impact": ""})
            elif isinstance(it, dict):
                out.append({
                    "risk": it.get("risk", it.get("item", "")),
                    "impact": it.get("impact", ""),
                })
        return out

    @classmethod
    def validate_extract(cls, data: dict[str, Any]) -> dict[str, Any]:
        missing = REQUIRED_KEYS - set(data.keys())
        if missing:
            raise ValueError(f"Extract JSON missing keys: {sorted(missing)}")

        defaults: dict[str, Any] = {
            "meeting_date": "",
            "meeting_title": "",
            "attendees": [],
            "attendee_count": None,
            "at_a_glance": [],
            "decisions": [],
            "closures": [],
            "new_tracks": [],
            "reassignments": [],
            "watch_list": [],
            "gaps": [],
            "next_steps": [],
            "team_members": [],
            "pillar_name": "",
        }
        merged = {**defaults, **data}
        for key in list(defaults.keys()):
            if merged.get(key) is None:
                merged[key] = defaults[key]
        # Normalize enriched sections for backward compatibility
        merged["decisions"] = cls._normalize_decisions(merged["decisions"])
        merged["closures"] = cls._normalize_with_summary(merged["closures"], key="item")
        merged["new_tracks"] = cls._normalize_with_summary(merged["new_tracks"], key="item")
        merged["watch_list"] = cls._normalize_risks(merged["watch_list"])
        return merged

    def extract(self, notes: str, context: str = "") -> dict[str, Any]:
        user_parts = [f"## Zoom meeting notes\n\n{notes}"]
        if context.strip():
            user_parts.append(
                "## Prior archived summaries (reference only — do not merge)\n\n"
                + context
            )
        parsed = self._chat_and_parse_json(
            "extract",
            "\n\n".join(user_parts),
            required_keys=["meeting_title", "decisions", "team_members"],
        )
        return self.validate_extract(parsed)

    def render(self, extract: dict[str, Any], pillar_name: str = "") -> tuple[str, str]:
        data = self.validate_extract(extract)
        if pillar_name:
            data["pillar_name"] = pillar_name
        data["meeting_title"] = data.get("meeting_title") or "Weekly Summary"
        markdown = self._render_markdown(data)
        template = self._jinja.get_template("onepager.html.j2")
        footer_text = os.getenv(
            "SUMMARY_FOOTER", "Generated by Rally · Process & Solutions Enablement"
        )
        org_label = os.getenv("RALLY_ORG_LABEL", "PaSE")
        generated_at = pacific_now_display()
        html = template.render(
            **data,
            markdown_sections=markdown,
            footer_text=footer_text,
            org_label=org_label,
            generated_at=generated_at,
        )
        return markdown, html

    def summarize(self, notes: str, context: str = "", pillar_name: str = "") -> OnePagerResult:
        extract = self.extract(notes, context=context)
        if pillar_name:
            extract["pillar_name"] = pillar_name
        markdown, html = self.render(extract, pillar_name=pillar_name)
        return OnePagerResult(extract=extract, markdown=markdown, html=html)

    def rerender_from_edited(self, edited_extract: dict[str, Any]) -> OnePagerResult:
        """Re-render the one-pager from a manually edited extract dict without calling the LLM."""
        return self.summarize_from_extract(edited_extract)

    def generate_rollup(self, pillar_summaries: list[dict]) -> dict[str, Any]:
        """Synthesize multiple pillar markdown summaries into a leadership rollup extract.

        Each entry in pillar_summaries must have keys: ``pillar`` (str) and ``markdown`` (str).
        """
        if not pillar_summaries:
            raise ValueError("At least one pillar summary is required.")

        parts = []
        for entry in pillar_summaries:
            parts.append(f"## Pillar: {entry['pillar']}\n\n{entry['markdown']}")
        combined = "\n\n---\n\n".join(parts)

        rollup = self._chat_and_parse_json(
            "rollup",
            combined,
            required_keys=[
                "at_a_glance",
                "cross_pillar_dependencies",
                "shared_blockers",
                "decisions_needed",
                "pillar_highlights",
                "capacity_risks",
                "next_steps",
            ],
        )
        defaults: dict[str, Any] = {
            "rollup_date": "",
            "pillars_included": [],
            "at_a_glance": [],
            "cross_pillar_dependencies": [],
            "shared_blockers": [],
            "decisions_needed": [],
            "pillar_highlights": [],
            "capacity_risks": [],
            "next_steps": [],
        }
        return {**defaults, **rollup}

    def render_rollup(self, rollup: dict[str, Any]) -> tuple[str, str]:
        """Render a leadership rollup extract into markdown and HTML."""
        md = self._render_rollup_markdown(rollup)

        seen_canonical: set[str] = set()
        cleaned_highlights: list[dict[str, Any]] = []
        for ph in rollup.get("pillar_highlights", []):
            canonical = _canonicalize_pillar_name(ph.get("pillar", ""))
            if canonical and canonical in seen_canonical:
                continue
            if canonical:
                seen_canonical.add(canonical)
            cleaned_highlights.append({**ph, "pillar": canonical})

        missing_pillars = [p.name for p in PILLARS if p.name not in seen_canonical]

        template = self._jinja.get_template("rollup.html.j2")
        footer_text = os.getenv(
            "SUMMARY_FOOTER", "Generated by Rally · Process & Solutions Enablement"
        )
        org_label = os.getenv("RALLY_ORG_LABEL", "PaSE")
        generated_at = pacific_now_display()
        html = template.render(
            **{**rollup, "pillar_highlights": cleaned_highlights},
            missing_pillars=missing_pillars,
            footer_text=footer_text,
            org_label=org_label,
            generated_at=generated_at,
        )
        return md, html

    def _render_rollup_markdown(self, data: dict[str, Any]) -> str:
        lines: list[str] = ["# Leadership Rollup"]
        pillars = data.get("pillars_included", [])
        if pillars:
            lines.append(f"**Pillars:** {', '.join(pillars)}")
        date = data.get("rollup_date", "")
        if date:
            lines.append(f"**Date:** {_format_date(date)}")
        lines.append("")

        for section, label in [
            ("at_a_glance", "At a glance"),
            ("cross_pillar_dependencies", "Cross-pillar dependencies"),
            ("shared_blockers", "Shared blockers"),
            ("decisions_needed", "Decisions needed from leadership"),
            ("capacity_risks", "Capacity risks"),
        ]:
            items = data.get(section, [])
            if items:
                lines.append(f"## {label}")
                for item in items:
                    lines.append(f"- {item}")
                lines.append("")

        highlights = data.get("pillar_highlights", [])
        if highlights:
            lines.append("## Pillar highlights")
            for ph in highlights:
                lines.append(f"### {ph.get('pillar', '')}")
                for h in ph.get("highlights", []):
                    lines.append(f"- {h}")
            lines.append("")

        next_steps = data.get("next_steps", [])
        if next_steps:
            lines.append("## Next steps")
            for s in next_steps:
                due = s.get("due", "")
                owner = s.get("owner", "")
                meta = " · ".join(x for x in [owner, due] if x)
                suffix = f" ({meta})" if meta else ""
                lines.append(f"- {s.get('action', '')}{suffix}")
            lines.append("")

        return "\n".join(lines).strip() + "\n"

    def generate_org_rollup(self, pillar_summaries: list[dict]) -> dict[str, Any]:
        """Synthesize pillar summaries into a VP-level org rollup extract.

        Returns a dict with a ``format`` key of either ``"text"`` (the override
        produced free-form prose — body is in ``body_text``) or ``"structured"``
        (the override produced valid JSON with the expected schema keys).
        """
        if not pillar_summaries:
            raise ValueError("At least one pillar summary is required.")

        parts = [
            f"## Pillar: {e['pillar']}\n\n{e['markdown']}" for e in pillar_summaries
        ]
        combined = "\n\n---\n\n".join(parts)

        system = self.load_prompt("exec_rollup")
        system = f"{system}\n\n{ORG_SOURCE_BREADCRUMB_ADDENDUM}"
        raw = self._chat(system, combined)
        raw_stripped = raw.strip()

        if "{" not in raw_stripped:
            return {
                "format": "text",
                "rollup_date": "",
                "pillars_included": [s["pillar"] for s in pillar_summaries],
                "body_text": raw_stripped,
            }

        required_keys = ["headline", "top_priorities", "critical_issues", "decisions_needed"]
        parsed = self._parse_json(raw_stripped)
        if not self._any_required_key_present(parsed, required_keys):
            parsed = self._retry_json(
                "exec_rollup", system, combined,
                reason="schema", required_keys=required_keys,
            )

        defaults: dict[str, Any] = {
            "format": "structured",
            "rollup_date": "",
            "pillars_included": [],
            "headline": "",
            "top_priorities": [],
            "critical_issues": [],
            "decisions_needed": [],
        }
        return {**defaults, **parsed}

    def render_org_rollup(self, extract: dict[str, Any]) -> tuple[str, str]:
        """Render an org-level rollup extract into markdown and HTML.

        Delegates to the text renderer when ``extract['format'] == 'text'``
        (the override returned prose), or the structured renderer otherwise.
        """
        if extract.get("format") == "text":
            return self._render_org_rollup_text(extract)
        md = self._render_org_rollup_markdown(extract)
        template = self._jinja.get_template("org_rollup.html.j2")
        footer_text = os.getenv(
            "SUMMARY_FOOTER", "Generated by Rally · Process & Solutions Enablement"
        )
        org_label = os.getenv("RALLY_ORG_LABEL", "PaSE")
        generated_at = pacific_now_display()
        html = template.render(
            **extract,
            footer_text=footer_text,
            org_label=org_label,
            generated_at=generated_at,
        )
        return md, html

    def _render_org_rollup_text(self, extract: dict[str, Any]) -> tuple[str, str]:
        """Render a free-form text org rollup (produced when the override returns prose)."""
        body_text = (extract.get("body_text") or "").strip()
        pillars = extract.get("pillars_included") or []
        md_lines = ["# Org Level Rollup"]
        if pillars:
            md_lines.append(f"**Pillars:** {', '.join(pillars)}")
        md_lines.append("")
        md_lines.append(body_text)
        md = "\n".join(md_lines).strip() + "\n"

        template = self._jinja.get_template("org_rollup_text.html.j2")
        footer_text = os.getenv(
            "SUMMARY_FOOTER", "Generated by Rally · Process & Solutions Enablement"
        )
        org_label = os.getenv("RALLY_ORG_LABEL", "PaSE")
        generated_at = pacific_now_display()
        html = template.render(
            body_text=body_text,
            pillars_included=pillars,
            rollup_date=extract.get("rollup_date", ""),
            footer_text=footer_text,
            org_label=org_label,
            generated_at=generated_at,
        )
        return md, html

    def _render_org_rollup_markdown(self, data: dict[str, Any]) -> str:
        lines: list[str] = ["# Org Level Rollup"]
        pillars = data.get("pillars_included", [])
        if pillars:
            lines.append(f"**Pillars:** {', '.join(pillars)}")
        date = data.get("rollup_date", "")
        if date:
            lines.append(f"**Date:** {_format_date(date)}")
        lines.append("")

        headline = data.get("headline", "")
        if headline:
            lines.append(f"> {headline}")
            lines.append("")

        for section, label in [
            ("top_priorities", "Top Priorities"),
            ("critical_issues", "Critical Issues"),
            ("decisions_needed", "Decisions Needed"),
        ]:
            items = data.get(section, [])
            if items:
                lines.append(f"## {label}")
                for item in items:
                    lines.append(f"- {item}")
                lines.append("")

        return "\n".join(lines).strip() + "\n"

    def summarize_from_extract(self, extract: dict[str, Any]) -> OnePagerResult:
        data = self.validate_extract(extract)
        markdown, html = self.render(data, pillar_name=data.get("pillar_name", ""))
        return OnePagerResult(extract=data, markdown=markdown, html=html)

    def _render_markdown(self, data: dict[str, Any]) -> str:
        lines: list[str] = []
        title = data["meeting_title"]
        date = data.get("meeting_date") or ""
        attendees = data.get("attendees") or []
        attendee_count = data.get("attendee_count")
        lines.append(f"# {title}")
        if date:
            lines.append(f"**Date:** {_format_date(date)}")
        if attendees:
            lines.append(f"**Attendees ({len(attendees)}):** {', '.join(attendees)}")
        elif attendee_count:
            lines.append(f"**Attendees:** {attendee_count}")
        lines.append("")

        if data.get("at_a_glance"):
            lines.append("## At a glance")
            for item in data["at_a_glance"]:
                lines.append(f"- {item}")
            lines.append("")

        if data.get("decisions"):
            lines.append("## Decisions")
            for item in data["decisions"]:
                lines.append(f"- **{item.get('decision', '')}**")
                if item.get("context"):
                    lines.append(f"  - {item['context']}")
            lines.append("")

        if data.get("closures"):
            lines.append("## Wins / closures")
            for item in data["closures"]:
                owner = item.get("owner", "")
                suffix = f" ({owner})" if owner else ""
                lines.append(f"- **{item.get('item', '')}{suffix}**")
                if item.get("summary"):
                    lines.append(f"  - {item['summary']}")
            lines.append("")

        if data.get("new_tracks"):
            lines.append("## New tracks")
            for item in data["new_tracks"]:
                owner = item.get("owner", "")
                suffix = f" — {owner}" if owner else ""
                lines.append(f"- **{item.get('item', '')}{suffix}**")
                if item.get("summary"):
                    lines.append(f"  - {item['summary']}")
            lines.append("")

        if data.get("reassignments"):
            lines.append("## Reassignments")
            for item in data["reassignments"]:
                lines.append(
                    f"- {item.get('item', '')}: {item.get('from', '?')} → {item.get('to', '?')}"
                )
            lines.append("")

        if data.get("watch_list") or data.get("gaps"):
            lines.append("## Risks & Open Items")
            for item in (data.get("watch_list") or []):
                lines.append(f"- **{item.get('risk', '')}**")
                if item.get("impact"):
                    lines.append(f"  - {item['impact']}")
            for item in (data.get("gaps") or []):
                lines.append(f"- {item}")
            lines.append("")

        if data.get("next_steps"):
            lines.append("## Next steps")
            for item in data["next_steps"]:
                due = item.get("due", "")
                owner = item.get("owner", "")
                meta = " · ".join(x for x in [owner, due] if x)
                suffix = f" ({meta})" if meta else ""
                lines.append(f"- {item.get('action', '')}{suffix}")
            lines.append("")

        return "\n".join(lines).strip() + "\n"
