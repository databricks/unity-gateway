"""Focused tests for the label-triggered UG review bot."""

from __future__ import annotations

import importlib.util
import sys
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
