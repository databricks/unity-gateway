"""Tests for skills_usage.py, which reports downloaded skills to AI Gateway from a detached
reporter process."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import ucode.skills_usage as su
from ucode.skills_api import SkillRef

WS = "https://example.databricks.com"
TOKEN = "secret-token"
SKILL_ID = "6f1c4b7a-2d0e-4a8b-9c3f-5e7d1a2b3c4d"
UNREACHABLE_WORKSPACE = "https://127.0.0.1"
UNREACHABLE_REPORT_POST = "POST https://127.0.0.1/ai-gateway/skills:reportSkillUsage"

_REPORT_THEN_EXIT = f"""
import sys
from ucode.skills_api import SkillRef
from ucode.skills_usage import report_skill_usage_in_background

refs = [
    SkillRef("main", "default", f"skill-{{i}}", f"skill-{{i}}", skill_id={SKILL_ID!r})
    for i in range(int(sys.argv[1]))
]
report_skill_usage_in_background({UNREACHABLE_WORKSPACE!r}, {TOKEN!r}, refs)
"""


def ref(securable_name: str, skill_id: str | None = SKILL_ID) -> SkillRef:
    return SkillRef(
        catalog="main",
        schema="default",
        securable_name=securable_name,
        bundle_name=securable_name,
        skill_id=skill_id,
    )


class _ReporterStdin(io.StringIO):
    """A reporter's stdin that keeps what ug wrote once ug closes it."""

    def __init__(self):
        super().__init__()
        self.received: str | None = None

    def close(self) -> None:
        self.received = self.getvalue()
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


def isolated_debug_log(monkeypatch, home: Path) -> Path:
    """Point reporters started by this test at ``home`` with debug logging on."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("UCODE_DEBUG", "1")
    return home / ".ucode" / "debug.log"


def wait_for_report_posts(debug_log: Path, expected: int) -> int:
    deadline = time.monotonic() + 30
    posts = 0
    while time.monotonic() < deadline:
        if debug_log.exists():
            posts = debug_log.read_text(encoding="utf-8").count(UNREACHABLE_REPORT_POST)
        if posts >= expected:
            break
        time.sleep(0.1)
    return posts


class TestReportSkillUsageInBackground:
    def test_hands_skills_in_the_environment_and_the_token_on_stdin(self, reporters):
        su.report_skill_usage_in_background(WS, TOKEN, [ref("triage")])

        [reporter] = reporters
        env = reporter.kwargs.pop("env")
        assert reporter.args == [sys.executable, "-P", "-m", "ucode.skills_usage"]
        assert reporter.kwargs == {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "text": True,
            **su._DETACHED_POPEN_OPTIONS,
        }
        assert json.loads(env.pop(su._REPORT_REQUEST_ENV_VAR)) == {
            "workspace": WS,
            "skills": [{"full_name": "main.default.triage", "id": SKILL_ID}],
        }
        assert env == dict(os.environ)
        assert reporter.stdin.received == TOKEN

    def test_skills_without_an_id_start_no_reporter(self, reporters):
        su.report_skill_usage_in_background(WS, TOKEN, [ref("triage", skill_id=None)])

        assert reporters == []

    def test_a_reporter_that_fails_to_start_is_ignored(self, monkeypatch):
        def fail_to_start(args, **kwargs):
            raise OSError("Resource temporarily unavailable")

        monkeypatch.setattr(su.subprocess_cross_os, "popen", fail_to_start)

        su.report_skill_usage_in_background(WS, TOKEN, [ref("triage")])

    def test_a_reporter_that_exits_before_reading_its_token_is_ignored(self, monkeypatch):
        class ClosedPipe(io.StringIO):
            def write(self, text: str) -> int:
                raise BrokenPipeError

        monkeypatch.setattr(
            su.subprocess_cross_os,
            "popen",
            lambda args, **kwargs: SimpleNamespace(stdin=ClosedPipe()),
        )

        su.report_skill_usage_in_background(WS, TOKEN, [ref("triage")])


class TestDetachedReporter:
    def test_a_large_request_arrives_after_ug_exits_right_away(self, monkeypatch, tmp_path):
        debug_log = isolated_debug_log(monkeypatch, tmp_path)

        subprocess.run(
            [sys.executable, "-c", _REPORT_THEN_EXIT, "1000"],
            cwd=tmp_path,
            timeout=60,
            check=True,
        )

        assert wait_for_report_posts(debug_log, 1) == 1

    def test_a_ucode_package_in_the_working_directory_is_not_run(self, monkeypatch, tmp_path):
        debug_log = isolated_debug_log(monkeypatch, tmp_path / "home")
        stolen = tmp_path / "stolen-token"
        shadowing_package = tmp_path / "project" / "ucode"
        shadowing_package.mkdir(parents=True)
        (shadowing_package / "__init__.py").write_text("")
        (shadowing_package / "skills_usage.py").write_text(
            f"import sys\nopen({str(stolen)!r}, 'w').write(sys.stdin.read())\n"
        )
        monkeypatch.chdir(shadowing_package.parent)

        su.report_skill_usage_in_background(UNREACHABLE_WORKSPACE, TOKEN, [ref("triage")])

        assert wait_for_report_posts(debug_log, 1) == 1
        assert not stolen.exists()


def capture_posts(monkeypatch, *, failure: str | None = None) -> list[dict]:
    posts: list[dict] = []

    def fake_post(url, token, payload, **kwargs):
        posts.append({"url": url, "token": token, "payload": payload, **kwargs})
        return (None, failure) if failure else ({}, None)

    monkeypatch.setattr(su, "_http_post_json", fake_post)
    return posts


def skill_entries(count: int) -> list[dict]:
    return [{"full_name": f"main.default.skill-{i}", "id": SKILL_ID} for i in range(count)]


def run_reporter(monkeypatch, skills: list[dict]) -> None:
    request = json.dumps({"workspace": WS, "skills": skills})
    monkeypatch.setenv(su._REPORT_REQUEST_ENV_VAR, request)
    monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN))
    su.main()


class TestReporterMain:
    def test_posts_full_name_and_id_with_ucode_user_agent(self, monkeypatch):
        posts = capture_posts(monkeypatch)
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

    def test_sends_one_report_per_fifty_skills(self, monkeypatch):
        posts = capture_posts(monkeypatch)

        run_reporter(monkeypatch, skill_entries(101))

        assert [len(post["payload"]["skills"]) for post in posts] == [50, 50, 1]

    def test_stops_after_the_first_failed_report(self, monkeypatch):
        posts = capture_posts(monkeypatch, failure="network error: timed out")

        run_reporter(monkeypatch, skill_entries(101))

        assert [len(post["payload"]["skills"]) for post in posts] == [50]
