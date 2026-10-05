"""Tests for skills_usage.py, which reports downloaded skills to AI Gateway from a detached
reporter process."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import ucode.skills_usage as su
from ucode.skills_api import SkillRef

WS = "https://example.databricks.com"
TOKEN = "secret-token"
SKILL_ID = "6f1c4b7a-2d0e-4a8b-9c3f-5e7d1a2b3c4d"


def ref(securable_name: str, skill_id: str | None = SKILL_ID) -> SkillRef:
    return SkillRef(
        catalog="main",
        schema="default",
        securable_name=securable_name,
        bundle_name=securable_name,
        skill_id=skill_id,
    )


class _ReporterStdin(io.StringIO):
    """A reporter's stdin that keeps what ug wrote, optionally blocking writes until released."""

    def __init__(self, release: threading.Event | None = None):
        super().__init__()
        self.release = release
        self.request: str | None = None
        self.closed_by_ug = threading.Event()

    def write(self, text: str) -> int:
        if self.release is not None:
            self.release.wait(timeout=5)
        return super().write(text)

    def close(self) -> None:
        self.request = self.getvalue()
        self.closed_by_ug.set()
        super().close()


@pytest.fixture
def reporters(monkeypatch) -> list[SimpleNamespace]:
    """Record each reporter ug starts instead of starting a process."""
    started: list[SimpleNamespace] = []

    def fake_popen(args, **kwargs):
        reporter = SimpleNamespace(args=args, kwargs=kwargs, stdin=_ReporterStdin())
        started.append(reporter)
        return reporter

    monkeypatch.setattr(su.subprocess_cross_os, "popen", fake_popen)
    return started


class TestReportSkillUsageInBackground:
    def test_hands_the_request_to_a_detached_reporter_over_stdin(self, reporters):
        su.report_skill_usage_in_background(WS, TOKEN, [ref("triage")])

        [reporter] = reporters
        assert reporter.args == [sys.executable, "-m", "ucode.skills_usage"]
        assert reporter.kwargs == {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "text": True,
            **su._DETACHED_POPEN_OPTIONS,
        }
        assert reporter.stdin.closed_by_ug.wait(timeout=5)
        assert json.loads(reporter.stdin.request) == {
            "workspace": WS,
            "token": TOKEN,
            "skills": [{"full_name": "main.default.triage", "id": SKILL_ID}],
        }

    def test_returns_before_the_reporter_reads_the_request(self, monkeypatch):
        release = threading.Event()
        stdin = _ReporterStdin(release)
        monkeypatch.setattr(
            su.subprocess_cross_os, "popen", lambda args, **kwargs: SimpleNamespace(stdin=stdin)
        )

        su.report_skill_usage_in_background(WS, TOKEN, [ref("triage")])

        assert not stdin.closed_by_ug.is_set()
        release.set()
        assert stdin.closed_by_ug.wait(timeout=5)

    def test_skills_without_an_id_start_no_reporter(self, reporters):
        su.report_skill_usage_in_background(WS, TOKEN, [ref("triage", skill_id=None)])

        assert reporters == []

    def test_a_reporter_that_fails_to_start_is_ignored(self, monkeypatch):
        def fail_to_start(args, **kwargs):
            raise OSError("Resource temporarily unavailable")

        monkeypatch.setattr(su.subprocess_cross_os, "popen", fail_to_start)

        su.report_skill_usage_in_background(WS, TOKEN, [ref("triage")])

    def test_a_reporter_that_exits_before_reading_is_ignored(self):
        class ClosedPipe(io.StringIO):
            def write(self, text: str) -> int:
                raise BrokenPipeError

        su._write_request(ClosedPipe(), "{}")


def capture_failing_posts(monkeypatch) -> list[dict]:
    posts: list[dict] = []

    def fake_post(url, token, payload, **kwargs):
        posts.append({"url": url, "token": token, "payload": payload, **kwargs})
        return None, "HTTP 429 Too Many Requests"

    monkeypatch.setattr(su, "_http_post_json", fake_post)
    return posts


def run_reporter(monkeypatch, skills: list[dict]) -> None:
    request = {"workspace": WS, "token": TOKEN, "skills": skills}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    su.main()


class TestReporterMain:
    def test_posts_full_name_and_id_with_ucode_user_agent(self, monkeypatch):
        posts = capture_failing_posts(monkeypatch)
        monkeypatch.setattr(su, "ug_version", lambda: "1.2.3")
        skills = [{"full_name": "main.default.triage", "id": SKILL_ID}]

        run_reporter(monkeypatch, skills)

        assert posts == [
            {
                "url": "https://example.databricks.com/ai-gateway/skills:reportSkillUsage",
                "token": TOKEN,
                "payload": {"skills": skills},
                "timeout": 10,
                "headers": {"User-Agent": "ucode/1.2.3"},
            }
        ]

    def test_sends_every_batch_of_fifty_despite_failures(self, monkeypatch):
        posts = capture_failing_posts(monkeypatch)

        run_reporter(
            monkeypatch,
            [{"full_name": f"main.default.skill-{i}", "id": SKILL_ID} for i in range(51)],
        )

        assert [len(post["payload"]["skills"]) for post in posts] == [50, 1]

    def test_runs_as_a_module_sending_the_request_from_its_stdin(self, tmp_path):
        request = {
            "workspace": "https://127.0.0.1",
            "token": TOKEN,
            "skills": [{"full_name": "main.default.triage", "id": SKILL_ID}],
        }
        home = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}

        result = subprocess.run(
            [sys.executable, "-m", "ucode.skills_usage"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, **home, "UCODE_DEBUG": "1"},
        )

        assert result.returncode == 0
        debug_log = (tmp_path / ".ucode" / "debug.log").read_text(encoding="utf-8")
        assert "POST https://127.0.0.1/ai-gateway/skills:reportSkillUsage" in debug_log
