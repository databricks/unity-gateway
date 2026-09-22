from __future__ import annotations

import csv
import multiprocessing
import os
import time
from pathlib import Path

from ucode import subagent_usage as usage


def _row(*, session_id: str = "session-1", agent_id: str = "agent-1"):
    return usage.SubagentUsageRow(
        recorded_at_utc="2027-01-15T08:00:00+00:00",
        session_id=session_id,
        agent_id=agent_id,
        subagent_name="worker",
        main_model="main-model",
        subagent_model="child-model",
        input_tokens=2,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=5,
        output_tokens=7,
        total_tokens=17,
        status="ok",
    )


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_row_in_process(app_dir: str, agent_id: str, start_event) -> None:
    usage.APP_DIR = Path(app_dir)
    start_event.wait()
    usage.write_subagent_usage(_row(agent_id=agent_id))


def test_default_directory_is_ucode_token_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "APP_DIR", tmp_path / ".ucode")

    assert usage.usage_directory() == tmp_path / ".ucode" / "token-logs"


def test_writes_private_session_file(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "usage_directory", lambda: tmp_path / "usage")

    path = usage.write_subagent_usage(_row(), now=1_800_000_000)

    assert path.is_file()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_appends_rows_to_session_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "usage_directory", lambda: tmp_path / "usage")
    first = _row(agent_id="agent-1")
    second = _row(agent_id="agent-2")
    expected_first = {
        "recorded_at_utc": "2027-01-15T08:00:00+00:00",
        "session_id": "session-1",
        "agent_id": "agent-1",
        "subagent_name": "worker",
        "main_model": "main-model",
        "subagent_model": "child-model",
        "input_tokens": "2",
        "cache_creation_input_tokens": "3",
        "cache_read_input_tokens": "5",
        "output_tokens": "7",
        "total_tokens": "17",
        "status": "ok",
    }
    expected_second = {**expected_first, "agent_id": "agent-2"}

    path = usage.write_subagent_usage(first, now=1_800_000_000)

    assert _read_rows(path) == [expected_first]

    usage.write_subagent_usage(second, now=1_800_000_001)

    assert _read_rows(path) == [expected_first, expected_second]


def test_concurrent_processes_append_complete_rows(tmp_path):
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    app_dir = tmp_path / ".ucode"
    processes = [
        context.Process(
            target=_write_row_in_process,
            args=(str(app_dir), f"agent-{index}", start_event),
        )
        for index in range(3)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join()

    assert [process.exitcode for process in processes] == [0, 0, 0]
    rows = _read_rows(app_dir / "token-logs" / "session-1.csv")
    assert sorted(row["agent_id"] for row in rows) == ["agent-0", "agent-1", "agent-2"]
    assert all(row["total_tokens"] == "17" and row["status"] == "ok" for row in rows)


def test_uses_independent_session_files(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "usage_directory", lambda: tmp_path / "usage")
    now = time.time()

    first = usage.write_subagent_usage(_row(), now=now)
    second = usage.write_subagent_usage(_row(session_id="session-2"), now=now + 1)

    assert first != second
    assert len(_read_rows(first)) == 1
    assert len(_read_rows(second)) == 1
