"""Environment configuration for Rally."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    databricks_host: str
    databricks_token: str
    model_endpoint: str
    volume_path: str
    local_dir: Path
    context_summary_count: int
    mock_llm: bool
    admin_emails: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Settings:
        admin_raw = os.getenv("PASE_TRACKER_ADMIN_EMAILS", "")
        admin_tuple = tuple(
            e.strip().lower() for e in admin_raw.split(",") if e.strip()
        )
        return cls(
            databricks_host=os.getenv("DATABRICKS_HOST", "").rstrip("/"),
            databricks_token=os.getenv("PASE_TRACKER_PAT", os.getenv("DATABRICKS_TOKEN", "")),
            model_endpoint=os.getenv(
                "DATABRICKS_MODEL_ENDPOINT", "databricks-claude-3-7-sonnet"
            ),
            volume_path=os.getenv(
                "PASE_TRACKER_VOLUME",
                os.getenv(
                    "MEETING_SUMMARY_VOLUME",
                    "/Volumes/main/default/pase_work_tracker",
                ),
            ),
            local_dir=Path(
                os.getenv(
                    "PASE_TRACKER_LOCAL_DIR",
                    os.getenv("MEETING_SUMMARY_LOCAL_DIR", "./data/pase_work_tracker"),
                )
            ),
            context_summary_count=int(os.getenv("CONTEXT_SUMMARY_COUNT", "3")),
            mock_llm=os.getenv("MOCK_LLM", "0") == "1",
            admin_emails=admin_tuple,
        )

    def is_admin(self, email: str | None) -> bool:
        if not email:
            return False
        return email.strip().lower() in self.admin_emails

    def llm_configured(self) -> bool:
        # Token may be omitted when running inside a Databricks App (SDK uses runtime auth).
        return bool(self.databricks_host) or self.mock_llm

    def validate_for_production(self) -> list[str]:
        issues: list[str] = []
        if not self.databricks_host:
            issues.append("DATABRICKS_HOST is not set")
        if not self.model_endpoint:
            issues.append("DATABRICKS_MODEL_ENDPOINT is not set")
        if not self.volume_path.startswith("/Volumes/"):
            issues.append("PASE_TRACKER_VOLUME must be a Unity Catalog volume path")
        return issues


def get_settings() -> Settings:
    return Settings.from_env()
