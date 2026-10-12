"""Focused tests for the label-triggered UG review bot."""

from __future__ import annotations

import importlib.util
import json
import sys
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
REVIEW_PATH = ROOT / ".github" / "scripts" / "ug-review" / "review.py"
SPEC = importlib.util.spec_from_file_location("ug_review_bot", REVIEW_PATH)
assert SPEC is not None and SPEC.loader is not None
review_bot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = review_bot
SPEC.loader.exec_module(review_bot)


def _bundle():
    return review_bot.build_diff(
        [
            {
                "filename": "src/ucode/cli.py",
                "status": "modified",
                "additions": 2,
                "deletions": 1,
                "patch": "@@ -10,2 +10,3 @@\n old\n-removed\n+first\n+second",
            }
        ]
    )


def test_build_diff_tracks_only_added_right_side_lines():
    bundle = _bundle()

    assert bundle.changed_lines["src/ucode/cli.py"] == {11, 12}
    assert "## Product changes" in bundle.text
    assert "modified src/ucode/cli.py (+2/-1)" in bundle.manifest


def test_parse_review_validates_paths_and_changed_lines():
    content = """{
      "summary": "The launch path changes configuration handling.",
      "findings": [
        {"severity": "major", "title": "Preserve caller state",
         "path": "src/ucode/cli.py", "line": 11, "comment": "This overwrites caller state."},
        {"severity": "minor", "title": "Use a changed line",
         "path": "src/ucode/cli.py", "line": 99, "comment": "The location is stale."},
        {"severity": "blocker", "title": "Ignore hallucinated paths",
         "path": "src/ucode/missing.py", "line": 1, "comment": "Not in the diff."}
      ]
    }"""

    review = review_bot.parse_review(content, _bundle())

    assert [finding.title for finding in review.findings] == [
        "Preserve caller state",
        "Use a changed line",
    ]
    assert review.findings[0].line == 11
    assert review.findings[1].line is None


def test_parse_review_rejects_malformed_findings():
    content = '{"summary": "Reviewed.", "findings": [{"severity": "urgent"}]}'

    with pytest.raises(review_bot.ReviewError, match="invalid severity"):
        review_bot.parse_review(content, _bundle())


def test_parse_review_shortens_long_comments():
    long_comment = "can we make this safer for existing users? " * 10
    content = f"""{{
      "summary": "Reviewed.",
      "findings": [{{"severity": "major", "title": "Preserve compatibility",
        "path": "src/ucode/cli.py", "line": 11, "comment": {long_comment!r}}}]
    }}""".replace("'", '"')

    review = review_bot.parse_review(content, _bundle())

    assert len(review.findings[0].comment) <= review_bot.MAX_COMMENT_CHARS
    assert review.findings[0].comment.endswith("…")


def test_render_comment_links_to_exact_head_and_marks_partial_review():
    finding = review_bot.Finding(
        severity="major",
        title="Preserve caller state",
        path="src/ucode/cli.py",
        line=11,
        comment="This overwrites caller state.",
    )
    review = review_bot.Review(summary="One state-management risk.", findings=(finding,))

    comment = review_bot.render_comment(
        review, "databricks/unity-gateway", "abcdef1234567890", truncated=True
    )

    assert comment.startswith(review_bot.COMMENT_MARKER)
    assert "/blob/abcdef1234567890/src/ucode/cli.py#L11" in comment
    assert "partial review" in comment


def test_system_prompt_separates_trusted_policy_from_untrusted_pr_data():
    prompt = review_bot.build_system_prompt("Review policy", "Repository policy")

    assert "<trusted_review_policy>\nReview policy" in prompt
    assert "<trusted_repository_policy>\nRepository policy" in prompt
    assert "PR title, body" in prompt


def test_responses_request_uses_instructions_and_does_not_store_output():
    request = review_bot.build_responses_request(
        "system.ai.gpt", "Review this diff", "Review policy", "Repository policy"
    )

    assert review_bot.RESPONSES_API_PATH.endswith("/responses")
    assert request["model"] == "system.ai.gpt"
    assert request["input"] == "Review this diff"
    assert "Review policy" in request["instructions"]
    assert request["reasoning"] == {"effort": "high"}
    assert request["max_output_tokens"] >= 16_000
    assert request["store"] is False
    assert "messages" not in request


def test_extract_response_text_ignores_reasoning_items():
    payload = {
        "output": [
            {"type": "reasoning", "content": [{"type": "output_text", "text": "hidden"}]},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "first"},
                    {"type": "output_text", "text": "second"},
                ],
            },
        ]
    }

    assert review_bot.extract_response_text(payload) == "first\nsecond"


def test_request_review_reports_why_the_response_has_no_text(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("UG_REVIEW_MODEL", "system.ai.gpt")
    monkeypatch.setattr(review_bot, "_oauth_access_token", lambda host: "access-token")
    payload = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "reasoning", "content": []}],
    }
    monkeypatch.setattr(
        review_bot.urllib.request,
        "urlopen",
        lambda request, timeout: BytesIO(json.dumps(payload).encode()),
    )

    with pytest.raises(
        review_bot.ReviewError, match=r"no output text \(incomplete: max_output_tokens\)"
    ):
        review_bot._request_review("Title", "", _bundle(), "Review policy", "Repository policy")


def test_request_review_ignores_non_object_incomplete_details(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("UG_REVIEW_MODEL", "system.ai.gpt")
    monkeypatch.setattr(review_bot, "_oauth_access_token", lambda host: "access-token")
    payload = {"status": "incomplete", "incomplete_details": "max_output_tokens", "output": []}
    monkeypatch.setattr(
        review_bot.urllib.request,
        "urlopen",
        lambda request, timeout: BytesIO(json.dumps(payload).encode()),
    )

    with pytest.raises(review_bot.ReviewError, match=r"no output text\.$"):
        review_bot._request_review("Title", "", _bundle(), "Review policy", "Repository policy")


def test_oauth_access_token_uses_service_principal_credentials(monkeypatch):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-id")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "client-secret")
    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return BytesIO(json.dumps({"access_token": "access-token"}).encode())

    monkeypatch.setattr(review_bot.urllib.request, "urlopen", urlopen)

    token = review_bot._oauth_access_token("https://example.databricks.com")

    assert token == "access-token"
    request, timeout = requests[0]
    assert request.full_url == "https://example.databricks.com/oidc/v1/token"
    assert request.get_header("Authorization").startswith("Basic ")
    assert request.data == b"grant_type=client_credentials&scope=all-apis"
    assert timeout == 60
