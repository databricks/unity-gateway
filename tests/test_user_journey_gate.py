"""Focused tests for the LLM-backed user-journey CI gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
GATE_PATH = ROOT / ".github" / "scripts" / "user-journey-required" / "check.py"
SPEC = importlib.util.spec_from_file_location("user_journey_gate", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_diff_reserves_space_for_integration_coverage():
    files = [
        {"filename": "src/ucode/cli.py", "status": "modified", "patch": "+product\n" * 50_000},
        {
            "filename": "tests/integration/test_ug_new_journey.py",
            "status": "added",
            "patch": "+journey coverage",
        },
    ]

    diff = gate.build_diff(files)

    assert "test_ug_new_journey.py" in diff
    assert "+journey coverage" in diff
    assert len(diff.encode()) <= gate.MAX_DIFF_BYTES + 100


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"needs_test": true, "reason": "new configure flow"}', (True, "new configure flow")),
        (
            '```json\n{"needs_test": false, "reason": "internal refactor"}\n```',
            (False, "internal refactor"),
        ),
    ],
)
def test_parse_verdict(content, expected):
    assert gate.parse_verdict(content) == expected


def test_parse_verdict_fails_closed_without_boolean():
    with pytest.raises(gate.GateError, match="boolean needs_test"):
        gate.parse_verdict('{"needs_test": "false", "reason": "trust me"}')


def test_system_prompt_includes_trusted_test_policy():
    prompt = gate.build_system_prompt("Never use mocks in integration tests.")

    assert "<trusted_tests_policy>" in prompt
    assert "Never use mocks in integration tests." in prompt
    assert "do not exempt a newly introduced journey" in prompt


def test_waiver_requires_exact_comment_by_allowlisted_admin():
    comments = [{"body": gate.WAIVER_COMMENT, "user": {"login": "rohita5l"}}]

    effective, reason = gate._waiver_status(comments, {"rohita5l", "lilly-luo"})

    assert effective is True
    assert "@rohita5l" in reason


def test_waiver_rejects_comment_from_other_collaborator():
    comments = [{"body": gate.WAIVER_COMMENT, "user": {"login": "other-collaborator"}}]

    effective, reason = gate._waiver_status(comments, {"rohita5l", "lilly-luo"})

    assert effective is False
    assert "unauthorized" in reason


def test_waiver_command_must_match_exactly():
    comments = [
        {"body": f"please {gate.WAIVER_COMMENT}", "user": {"login": "rohita5l"}},
        {"body": "skip it", "user": {"login": "lilly-luo"}},
    ]

    assert gate._waiver_status(comments, {"rohita5l", "lilly-luo"}) == (False, "")
