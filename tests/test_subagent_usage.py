from __future__ import annotations

import csv
import os
import time
from pathlib import Path

from ucode import subagent_usage as usage


def _row(*, session_id: str = "session-1", agent_id: str = "agent-1"):
    row = usage.SubagentUsageRow.build(
        {
            "session_id": session_id,
            "agent_id": agent_id,
            "subagent_name": "worker",
            "main_model": "main-model",
            "subagent_model": "child-model",
            "input_tokens": 2,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 5,
            "output_tokens": 7,
            "status": "ok",
        },
        now=1_800_000_000,
    )
    assert row is not None
    return row


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_builds_typed_row_and_derives_schema_and_total():
    row = _row()

    assert row.recorded_at_utc == "2027-01-15T08:00:00+00:00"
    assert row.total_tokens == 17
    assert tuple(row.to_csv_dict()) == usage.CSV_FIELDS


def test_build_requires_session_and_agent_ids():
    assert usage.SubagentUsageRow.build({}) is None
    assert usage.SubagentUsageRow.build({"session_id": "session-1"}) is None


def test_default_directory_is_ucode_token_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "APP_DIR", tmp_path / ".ucode")

    assert usage.usage_directory() == tmp_path / ".ucode" / "token-logs"


def test_writes_schema_and_deduplicates_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "usage_directory", lambda: tmp_path / "usage")
    row = _row()

    path = usage.write_subagent_usage(row, now=1_800_000_000)
    usage.write_subagent_usage(row, now=1_800_000_001)

    assert _read_rows(path) == [{field: str(getattr(row, field)) for field in usage.CSV_FIELDS}]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_uses_independent_session_files(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "usage_directory", lambda: tmp_path / "usage")
    now = time.time()

    first = usage.write_subagent_usage(_row(), now=now)
    second = usage.write_subagent_usage(_row(session_id="session-2"), now=now + 1)

    assert first != second
    assert len(_read_rows(first)) == 1
    assert len(_read_rows(second)) == 1


def test_removes_session_files_inactive_for_more_than_seven_days(tmp_path, monkeypatch):
    directory = tmp_path / "usage"
    directory.mkdir()
    monkeypatch.setattr(usage, "usage_directory", lambda: directory)
    stale = directory / "stale.csv"
    stale.write_text("old", encoding="utf-8")
    os.utime(stale, (1, 1))

    usage.write_subagent_usage(_row(), now=usage.RETENTION_SECONDS + 2)

    assert not stale.exists()


def test_compacts_one_session_file_to_newest_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "usage_directory", lambda: tmp_path / "usage")
    monkeypatch.setattr(usage, "MAX_SESSION_CSV_BYTES", 650)
    path = None
    for index in range(12):
        path = usage.write_subagent_usage(
            _row(agent_id=f"agent-{index}"), now=1_800_000_000 + index
        )

    assert path is not None
    rows = _read_rows(path)
    assert path.stat().st_size <= usage.MAX_SESSION_CSV_BYTES
    assert rows
    assert rows[-1]["agent_id"] == "agent-11"
    assert rows[0]["agent_id"] != "agent-0"


def test_hashes_unsafe_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "usage_directory", lambda: tmp_path)

    path = usage.session_csv_path("../../outside")

    assert path.parent == tmp_path
    assert path.name.endswith(".csv")
    assert ".." not in path.name
