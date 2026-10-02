"""Focused transcript and rendering checks for the budget-defaults evidence helpers."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.integration.utils.budget_evidence import (
    assert_budget_panel,
    assert_native_process_tree,
    assert_task_model,
    assert_usage_summary,
    parse_budget_panel,
    parse_usage_summary,
)


class _Session:
    def __init__(self, home):
        self.home = home
        self.records = {}

    def record(self, name, value):
        self.records[name] = value


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _claude_answer(value, model):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": value}],
        },
    }


def _codex_turn(turn_id, model):
    return {"type": "turn_context", "payload": {"turn_id": turn_id, "model": model}}


def _codex_complete(turn_id, value):
    return {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": turn_id,
            "last_agent_message": value,
        },
    }


def test_budget_panel_uses_decimal_boundary_math_and_native_float_display():
    filled = "█" * 14
    empty = "░" * 14
    transcript = f"│$2.67 / $5.35    50% used│\n│{filled}{empty}│\n"
    raw = {"current_spend": "2.675", "effective_threshold": "5.35"}

    panel = assert_budget_panel(transcript, raw, "fractional-cent")

    assert panel.spend == Decimal("2.67")
    assert panel.threshold == Decimal("5.35")
    assert panel.percent == 50
    assert parse_budget_panel(transcript) == panel


def test_budget_panel_matches_product_float_rounding_at_62_5_percent():
    filled = "█" * 17
    empty = "░" * 11
    transcript = f"│$1.69 / $2.71    62% used│\n│{filled}{empty}│\n"
    raw = {"current_spend": "1.6914364", "effective_threshold": "2.706298240000000000"}

    panel = assert_budget_panel(transcript, raw, "float-boundary")

    assert panel.percent == 62


def test_usage_summary_matches_backend_values_and_thirty_cell_meter():
    filled = "█" * 18
    empty = "░" * 12
    transcript = f"Budget spend: $2.68 of $4.28 (62%)\n[{filled}{empty}]\n"
    raw = {"current_spend": "2.675", "effective_threshold": "4.28"}

    summary = assert_usage_summary(transcript, raw, "usage")

    assert summary.spend == Decimal("2.68")
    assert summary.threshold == Decimal("4.28")
    assert summary.percent == 62
    assert parse_usage_summary(transcript) == summary


@pytest.mark.parametrize(
    ("claude_model", "codex_model"),
    [
        ("system.ai.claude-sonnet-4-6", "system.ai.gpt-5-6-sol"),
        ("claude-sonnet-4-6", "gpt-5.6-sol"),
    ],
)
def test_task_model_is_bound_to_the_completed_native_answer(tmp_path, claude_model, codex_model):
    session = _Session(tmp_path)
    claude_value = "claude-task-value"
    _write_jsonl(
        tmp_path / ".claude/projects/project.jsonl",
        [_claude_answer("old answer", "stale-model"), _claude_answer(claude_value, claude_model)],
    )
    _write_jsonl(
        tmp_path / ".claude/projects/project/subagents/child.jsonl",
        [_claude_answer(claude_value, "wrong-child-model")],
    )

    assert (
        assert_task_model(
            session,
            "claude",
            SimpleNamespace(value=claude_value),
            "system.ai.claude-sonnet-4-6",
            "claude-task",
        )
        == claude_model
    )

    codex_value = "codex-task-value"
    _write_jsonl(
        tmp_path / ".codex/sessions/2026/rollout.jsonl",
        [
            _codex_turn("old-turn", "system.ai.gpt-5-5"),
            _codex_complete("old-turn", "old answer"),
            _codex_turn("target-turn", codex_model),
            _codex_complete("target-turn", codex_value),
        ],
    )
    _write_jsonl(
        tmp_path / ".codex/sessions/2026/child.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"source": {"subagent": {"thread_spawn": {}}}},
            },
            _codex_turn("child-turn", "wrong-child-model"),
            _codex_complete("child-turn", codex_value),
        ],
    )

    assert (
        assert_task_model(
            session,
            "codex",
            SimpleNamespace(value=codex_value),
            "system.ai.gpt-5-6-sol",
            "codex-task",
        )
        == codex_model
    )


@pytest.mark.parametrize(
    "records",
    [
        [
            _codex_turn("stale-turn", "system.ai.gpt-5-5"),
            _codex_complete("target-turn", "codex-mismatched-context"),
        ],
        [
            _codex_complete("target-turn", "codex-later-context"),
            _codex_turn("target-turn", "system.ai.gpt-5-6-sol"),
        ],
        [_codex_turn("target-turn", "system.ai.gpt-5-6-sol"), _codex_complete(None, "no-id")],
    ],
)
def test_codex_task_model_rejects_mismatched_or_later_context(tmp_path, records):
    _write_jsonl(tmp_path / ".codex/sessions/2026/rollout.jsonl", records)
    value = next(
        record["payload"]["last_agent_message"]
        for record in records
        if record.get("type") == "event_msg"
    )

    with pytest.raises(AssertionError, match="no preceding model context"):
        assert_task_model(
            _Session(tmp_path),
            "codex",
            SimpleNamespace(value=value),
            "system.ai.gpt-5-6-sol",
            "invalid-codex-context",
        )


@pytest.mark.parametrize(
    ("agent", "argv", "executable_path"),
    [
        (
            "claude",
            ["python", "/tmp/claude-case/bin/python", "claude"],
            "/tmp/claude-case/bin/python",
        ),
        (
            "codex",
            ["python", "/tmp/codex-case/bin/python", "codex"],
            "/tmp/codex-case/bin/python",
        ),
    ],
)
def test_native_process_tree_rejects_ug_argument_and_unrelated_bin(agent, argv, executable_path):
    tree = {
        "root_pid": 123,
        "descendant_pids": [123],
        "nodes": [
            {
                "pid": 123,
                "ppid": 1,
                "argv": argv,
                "command": " ".join(argv),
                "executable": argv[0],
                "executable_path": executable_path,
            }
        ],
    }

    with pytest.raises(AssertionError, match="no native"):
        assert_native_process_tree(tree, agent, "false-native")


@pytest.mark.parametrize(
    ("agent", "package_script"),
    [
        ("claude", "/node_modules/@anthropic-ai/claude-code/cli.js"),
        ("codex", "/node_modules/@openai/codex/bin/codex.js"),
    ],
)
def test_native_process_tree_accepts_published_npm_script(agent, package_script):
    tree = {
        "root_pid": 123,
        "descendant_pids": [123],
        "nodes": [
            {
                "pid": 123,
                "ppid": 1,
                "argv": ["node", "/opt/npm" + package_script],
                "command": "node /opt/npm" + package_script,
                "executable": "node",
                "executable_path": "/usr/bin/node",
            }
        ],
    }

    result = assert_native_process_tree(tree, agent, "published-native")
    assert result["native_candidates"][0]["pid"] == 123
