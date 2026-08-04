"""Local storage tests (no Databricks required)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings
from storage import MeetingStorage, read_uploaded_text


@pytest.fixture
def local_storage(tmp_path: Path) -> MeetingStorage:
    settings = Settings(
        databricks_host="",
        databricks_token="",
        model_endpoint="test",
        volume_path="/Volumes/main/default/meeting_summaries",
        local_dir=tmp_path / "data",
        context_summary_count=2,
        mock_llm=True,
    )
    return MeetingStorage(settings)


def test_save_and_list_summary(local_storage: MeetingStorage) -> None:
    record = local_storage.save_summary(
        meeting_title="Weekly Standup",
        meeting_date="2026-05-06",
        markdown="# Weekly Standup\n",
        html="<html></html>",
        extract={"meeting_title": "Weekly Standup"},
        upload_path=None,
        source_filename="notes.txt",
    )
    records = local_storage.list_summaries()
    assert len(records) == 1
    assert records[0].id == record.id
    assert "Weekly Standup" in local_storage.read_file(record.markdown_path)


def test_read_uploaded_text_utf8() -> None:
    text = read_uploaded_text("notes.txt", b"Hello Zoom notes")
    assert text == "Hello Zoom notes"
