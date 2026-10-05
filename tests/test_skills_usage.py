"""Tests for skills_usage.py, the fire-and-forget skill usage reports to AI Gateway."""

from __future__ import annotations

import threading

import ucode.skills_usage as su
from ucode.skills_api import SkillRef

WS = "https://example.databricks.com"
SKILL_ID = "6f1c4b7a-2d0e-4a8b-9c3f-5e7d1a2b3c4d"


def ref(securable_name: str, skill_id: str | None = SKILL_ID) -> SkillRef:
    return SkillRef(
        catalog="main",
        schema="default",
        securable_name=securable_name,
        bundle_name=securable_name,
        skill_id=skill_id,
    )


def capture_failing_posts(monkeypatch) -> list[dict]:
    posts: list[dict] = []

    def fake_post(url, token, payload, **kwargs):
        posts.append({"url": url, "token": token, "payload": payload, **kwargs})
        return None, "HTTP 429 Too Many Requests"

    monkeypatch.setattr(su, "_http_post_json", fake_post)
    return posts


class TestReportSkillUsage:
    def test_posts_full_name_and_id_with_ucode_user_agent(self, monkeypatch):
        posts = capture_failing_posts(monkeypatch)
        monkeypatch.setattr(su, "ug_version", lambda: "1.2.3")

        su.report_skill_usage(WS, "tok", [ref("triage")])

        assert posts == [
            {
                "url": "https://example.databricks.com/ai-gateway/skills:reportSkillUsage",
                "token": "tok",
                "payload": {"skills": [{"full_name": "main.default.triage", "id": SKILL_ID}]},
                "timeout": 2,
                "headers": {"User-Agent": "ucode/1.2.3"},
            }
        ]

    def test_sends_every_batch_of_fifty_despite_failures(self, monkeypatch):
        posts = capture_failing_posts(monkeypatch)

        su.report_skill_usage(WS, "tok", [ref(f"skill-{i}") for i in range(51)])

        assert [len(post["payload"]["skills"]) for post in posts] == [50, 1]

    def test_skills_without_an_id_are_not_sent(self, monkeypatch):
        posts = capture_failing_posts(monkeypatch)

        su.report_skill_usage(WS, "tok", [ref("triage", skill_id=None)])

        assert posts == []


class TestReportSkillUsageInBackground:
    def test_returns_while_a_non_daemon_thread_sends_the_report(self, monkeypatch):
        release = threading.Event()
        reported: list[list[SkillRef]] = []

        def slow_report(workspace, token, refs):
            release.wait(timeout=5)
            reported.append(refs)

        monkeypatch.setattr(su, "report_skill_usage", slow_report)

        su.report_skill_usage_in_background(WS, "tok", [ref("triage")])

        [thread] = [t for t in threading.enumerate() if t.name == "skill-usage-report"]
        assert not thread.daemon
        assert reported == []
        release.set()
        thread.join(timeout=5)
        assert reported == [[ref("triage")]]
