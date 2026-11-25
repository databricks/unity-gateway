"""Keep observed model usage redacted and enforce lane model separation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.utils import traffic_ledger as ledger
from tests.integration.utils.harness import UserSession


def test_native_ledger_deduplicates_usage_and_does_not_copy_content():
    context = {
        "type": "turn_context",
        "payload": {"turn_id": "turn-1", "model": "system.ai.gpt-6-sol"},
    }
    usage = {
        "timestamp": "2026-11-25T00:00:01Z",
        "type": "token_usage_record",
        "payload": {
            "session_id": "session-1",
            "response_id": "response-1",
            "turn_id": "turn-1",
            "usage": {"input_tokens": 12, "cached_input_tokens": 7, "output_tokens": 3},
            "private": "secret prompt",
        },
    }
    claude = {
        "timestamp": "2026-11-25T00:00:02Z",
        "type": "assistant",
        "sessionId": "session-2",
        "message": {
            "id": "response-2",
            "model": "system.ai.claude-sonnet-4-6",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 11,
                "output_tokens": 4,
            },
            "content": "private answer",
        },
    }
    result = ledger.native_usage(
        {"private-path": [context, usage, usage]},
        {"other-private-path": [claude, claude]},
        workspace="managed-primary",
        known_models={"system.ai.gpt-6-sol", "system.ai.claude-sonnet-4-6"},
    )
    assert result["coverage"] == "partial"
    assert len(result["responses"]) == 2
    assert result["responses"][0]["usage"] == {
        "input_tokens": 12,
        "cached_input_tokens": 7,
        "output_tokens": 3,
    }
    assert result["responses"][1]["usage"]["cache_read_input_tokens"] == 11
    assert "private" not in repr(result) and "session-1" not in repr(result)


def test_duplicate_claude_response_keeps_latest_usage_counts():
    model = "claude-haiku-4-5-20251001"
    records = [
        {
            "type": "assistant",
            "timestamp": timestamp,
            "sessionId": "session",
            "message": {"id": "response", "model": model, "usage": usage},
        }
        for timestamp, usage in (
            ("2026-11-25T00:00:01Z", {"input_tokens": 4, "output_tokens": 1}),
            ("2026-11-25T00:00:02Z", {"input_tokens": 4, "output_tokens": 9}),
        )
    ]
    result = ledger.native_usage(
        {}, {"session": records}, workspace="staging", known_models={model}
    )
    assert len(result["responses"]) == 1
    assert result["responses"][0]["usage"]["output_tokens"] == 9
    assert result["responses"][0]["timestamp"] == "2026-11-25T00:00:02Z"


def test_staging_claude_native_usage_records_model_tokens_and_timestamp():
    model = "claude-haiku-4-5-20251001"
    record = {
        "type": "assistant",
        "timestamp": "2026-11-25T00:00:01Z",
        "sessionId": "private-session",
        "message": {
            "id": "private-response",
            "model": model,
            "usage": {"input_tokens": 11, "cache_read_input_tokens": 9, "output_tokens": 2},
            "content": "private-prompt",
        },
    }
    result = ledger.native_usage(
        {}, {"private-path": [record]}, workspace="staging", known_models={model}
    )
    assert result["coverage"] == "partial"
    assert result["responses"] == [
        {
            "source": "claude-native",
            "timestamp": "2026-11-25T00:00:01Z",
            "model": model,
            "reported_model": model,
            "workspace": "staging",
            "usage": {"input_tokens": 11, "cache_read_input_tokens": 9, "output_tokens": 2},
        }
    ]
    assert "private" not in repr(result)


def test_staging_lane_preserves_partial_unmapped_evidence(tmp_path):
    case = tmp_path / "artifacts/case"
    case.mkdir(parents=True)
    case.joinpath("model-ledger.json").write_text(
        json.dumps({"coverage": "unmapped", "model_coverage": "unmapped", "responses": []})
    )
    result = ledger.finish_lane(tmp_path, lane="staging")
    assert result["coverage"] == "unmapped"
    assert result["responses"] == []
    assert json.loads((tmp_path / "model-ledger.json").read_text()) == result


def test_staging_desktop_ledger_records_only_recent_native_usage(tmp_path):
    sessions = tmp_path / "sessions"
    new = sessions / "new/projects/session/new.jsonl"
    new.parent.mkdir(parents=True)
    rows = [
        {
            "type": "assistant",
            "timestamp": timestamp,
            "sessionId": "private-session",
            "message": {
                "id": identifier,
                "model": "claude-haiku-4-5-20251001",
                "usage": {"input_tokens": 4, "output_tokens": 2},
                "content": "private-content",
            },
        }
        for identifier, timestamp in (
            ("old", "2026-11-25T00:00:00Z"),
            ("new", "2026-11-25T00:00:02Z"),
        )
    ]
    new.write_text("\n".join(json.dumps(row) for row in rows) + "\n\n", encoding="utf-8")
    result = ledger.staging_desktop_usage(
        sessions, since="2026-11-25T00:00:01Z", model="claude-haiku-4-5-20251001"
    )
    assert result["coverage"] == "partial"
    assert len(result["responses"]) == 1
    assert result["responses"][0]["timestamp"] == "2026-11-25T00:00:02Z"
    assert "private" not in repr(result)


def test_staging_desktop_cli_writes_unmapped_evidence_when_configuration_missing(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    script = Path(ledger.__file__)
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(output),
            "--lane",
            "staging-desktop",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--since-file",
            str(tmp_path / "missing-configuration.json"),
            "--model",
            "claude-haiku-4-5-20251001",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads((output / "model-ledger.json").read_text())
    assert evidence["coverage"] == "unmapped"
    assert evidence["responses"] == []


def test_staging_desktop_missing_sessions_are_not_reported_as_zero_usage(tmp_path):
    result = ledger.staging_desktop_usage(
        tmp_path / "missing", since="2026-11-25T00:00:01Z", model="claude-haiku-4-5-20251001"
    )
    assert result["coverage"] == result["model_coverage"] == "unmapped"
    assert result["responses"] == []


def test_staging_desktop_no_response_is_unmapped(tmp_path):
    session = tmp_path / "one/projects/session/data.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"type": "user", "timestamp": "2026-11-25T00:00:02Z"}) + "\n")
    result = ledger.staging_desktop_usage(
        tmp_path, since="2026-11-25T00:00:01Z", model="claude-haiku-4-5-20251001"
    )
    assert result["coverage"] == "unmapped"


def test_staging_case_ledger_uses_requested_provider_model(tmp_path, monkeypatch):
    monkeypatch.setenv("UG_INTEGRATION_CLAUDE_PROVIDER_MODEL", "claude-haiku-4-5-20251001")
    project = tmp_path / "project"
    project.mkdir()
    session = UserSession(tmp_path / "root", project, tmp_path / "ug", tmp_path / "artifacts")
    path = session.home / ".claude/projects/session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-11-25T00:00:01Z",
                "sessionId": "private-session",
                "message": {
                    "id": "private-response",
                    "model": "claude-haiku-4-5-20251001",
                    "usage": {"input_tokens": 11, "output_tokens": 2},
                },
            }
        )
        + "\n"
    )
    ledger.record_case_usage(session, workspace="staging")
    result = json.loads((session.artifacts / "model-ledger.json").read_text())
    assert result["coverage"] == "partial"
    assert result["responses"][0]["model"] == "claude-haiku-4-5-20251001"


def test_case_ledger_reads_real_native_rollout_without_copying_prompts(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    session = UserSession(tmp_path / "root", project, tmp_path / "ug", tmp_path / "artifacts")
    rollout = session.home / ".codex/sessions/example.jsonl"
    rollout.parent.mkdir(parents=True)
    rows = [
        {"type": "turn_context", "payload": {"turn_id": "t", "model": "system.ai.gpt-5-6-sol"}},
        {
            "type": "token_usage_record",
            "timestamp": "2026-11-25T00:00:01Z",
            "payload": {
                "session_id": "s",
                "response_id": "r",
                "turn_id": "t",
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "prompt": "private-content",
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    ledger.record_case_usage(session, workspace="managed-primary")
    result = json.loads((session.artifacts / "model-ledger.json").read_text())
    assert result["coverage"] == "partial"
    assert result["responses"][0]["model"] == "system.ai.gpt-5-6-sol"
    assert "private-content" not in json.dumps(result)


def test_duplicate_response_with_different_models_is_unmapped():
    sessions = {
        "first": [
            {"type": "turn_context", "payload": {"turn_id": "a", "model": "system.ai.gpt-6-sol"}},
            {
                "type": "token_usage_record",
                "timestamp": "2026-11-25T00:00:01Z",
                "payload": {
                    "session_id": "s",
                    "response_id": "r",
                    "turn_id": "a",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ],
        "second": [
            {"type": "turn_context", "payload": {"turn_id": "b", "model": "system.ai.gpt-5-6-sol"}},
            {
                "type": "token_usage_record",
                "timestamp": "2026-11-25T00:00:02Z",
                "payload": {
                    "session_id": "s",
                    "response_id": "r",
                    "turn_id": "b",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ],
    }
    result = ledger.native_usage(
        sessions,
        {},
        workspace="managed-primary",
        known_models={"system.ai.gpt-6-sol", "system.ai.gpt-5-6-sol"},
    )
    assert result["coverage"] == "unmapped"


def test_secondary_workspace_claude_model_stays_visible_to_basic_guard(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    session = UserSession(tmp_path / "root", project, tmp_path / "ug", tmp_path / "artifacts/case")
    rollout = session.home / ".claude/projects/example.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-11-25T00:00:01Z",
                "sessionId": "s",
                "message": {
                    "id": "r",
                    "model": "system.ai.claude-opus-5-5[1m]",
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            }
        )
        + "\n"
    )
    ledger.record_case_usage(session, workspace=None)
    result = ledger.finish_lane(tmp_path, lane="basic-claude")
    assert result["coverage"] == "unmapped" and result["model_coverage"] == "partial"
    assert result["responses"][0]["reported_model"] == "system.ai.claude-opus-5-5[1m]"


def test_conflicting_models_for_one_turn_are_unmapped():
    sessions = {
        "file": [
            {"type": "turn_context", "payload": {"turn_id": "t", "model": "system.ai.gpt-6-sol"}},
            {"type": "turn_context", "payload": {"turn_id": "t", "model": "system.ai.gpt-5-6-sol"}},
            {
                "type": "token_usage_record",
                "timestamp": "2026-11-25T00:00:01Z",
                "payload": {
                    "session_id": "s",
                    "response_id": "r",
                    "turn_id": "t",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ]
    }
    result = ledger.native_usage(
        sessions,
        {},
        workspace="managed-primary",
        known_models={"system.ai.gpt-6-sol", "system.ai.gpt-5-6-sol"},
    )
    assert result["coverage"] == "unmapped"


def test_missing_turn_id_cannot_inherit_an_unidentified_context():
    sessions = {
        "file": [
            {"type": "turn_context", "payload": {"model": "system.ai.gpt-6-sol"}},
            {
                "type": "token_usage_record",
                "timestamp": "2026-11-25T00:00:01Z",
                "payload": {
                    "session_id": "s",
                    "response_id": "r",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ]
    }
    result = ledger.native_usage(
        sessions, {}, workspace="managed-primary", known_models={"system.ai.gpt-6-sol"}
    )
    assert result["coverage"] == "unmapped" and result["responses"][0]["model"] is None


def test_no_native_responses_stays_partial_not_complete():
    result = ledger.native_usage({}, {}, workspace="managed-primary", known_models=set())
    assert result["coverage"] == "partial" and result["responses"] == []


def test_native_ledger_marks_missing_model_and_workspace_unmapped():
    usage = {
        "timestamp": "2026-11-25T00:00:01Z",
        "type": "token_usage_record",
        "payload": {
            "session_id": "session-1",
            "response_id": "response-1",
            "turn_id": "unknown",
            "usage": {"input_tokens": 12, "output_tokens": 3},
        },
    }
    result = ledger.native_usage(
        {"path": [usage]}, {}, workspace=None, known_models={"system.ai.gpt-6-sol"}
    )
    assert result["coverage"] == "unmapped"
    assert result["responses"][0]["model"] is None
    assert result["responses"][0]["workspace"] is None


@pytest.mark.parametrize(
    ("lane", "observed", "accepted"),
    [
        ("smart", "gpt-6-luna", True),
        ("smart", "gpt-5-6-sol", False),
        ("desktop", "gpt-5-6-sol", True),
        ("desktop", "gpt-6-sol", False),
        ("desktop", "unknown-model", False),
    ],
)
def test_model_guard_uses_current_fixture_sets(lane, observed, accepted):
    desktop = {"system.ai.gpt-5-6-sol"}
    smart = {"system.ai.gpt-6-sol", "system.ai.gpt-6-luna"}
    event = {"model": observed, "workspace": "managed-primary"}
    if accepted:
        ledger.check_model_separation(
            [event], lane=lane, desktop_models=desktop, smart_models=smart
        )
    else:
        with pytest.raises(ValueError):
            ledger.check_model_separation(
                [event], lane=lane, desktop_models=desktop, smart_models=smart
            )


@pytest.mark.parametrize(
    ("lane", "reported", "accepted"),
    [
        ("basic-claude", "system.ai.claude-opus-5-5[1m]", True),
        ("basic-claude", "system.ai.gpt-6-sol", False),
        ("basic-codex", "system.ai.gpt-99", True),
        ("basic-codex", "system.ai.gpt-6-sol", False),
        ("basic-codex", "system.ai.gpt-5-6-sol", False),
        ("basic-codex", "system.ai.claude-opus-5-5", False),
    ],
)
def test_basic_lanes_reject_observed_cross_model_use(lane, reported, accepted):
    event = {"model": None, "reported_model": reported, "workspace": None}
    kwargs = {
        "lane": lane,
        "desktop_models": {"system.ai.gpt-5-6-sol"},
        "smart_models": {"system.ai.gpt-6-sol"},
    }
    if accepted:
        ledger.check_model_separation([event], **kwargs)
    else:
        with pytest.raises(ValueError):
            ledger.check_model_separation([event], **kwargs)


def test_basic_guard_rejects_conflicting_reported_and_canonical_models():
    event = {
        "model": "system.ai.gpt-6-sol",
        "reported_model": "system.ai.claude-opus-5-5",
        "workspace": "managed-primary",
    }
    with pytest.raises(ValueError, match="model"):
        ledger.check_model_separation(
            [event],
            lane="basic-claude",
            desktop_models={"system.ai.gpt-5-6-sol"},
            smart_models={"system.ai.gpt-6-sol"},
        )


def test_model_sets_follow_current_desktop_and_smart_fixtures(tmp_path):
    def write_fixture(name, model):
        (tmp_path / f"{name}.json").write_text(
            json.dumps(
                {
                    "enabled_agents": [
                        {
                            "agent": "CODING_AGENT_CODEX",
                            "config": {"models": {"model_services": [model]}},
                        }
                    ]
                }
            )
        )

    write_fixture("managed_workspace_default", "system.ai.new-desktop")
    write_fixture("codex_smart_routing", "system.ai.new-smart")
    desktop, smart = ledger.model_sets(tmp_path)
    assert desktop == {"system.ai.new-desktop"}
    assert smart == {"system.ai.new-smart"}
    with pytest.raises(ValueError, match="Overlapping model"):
        ledger.check_model_separation(
            [{"model": "new-desktop", "workspace": "managed-primary"}],
            lane="smart",
            desktop_models=desktop,
            smart_models=smart,
        )


def test_smart_lane_rejects_missing_model_coverage(tmp_path):
    case = tmp_path / "artifacts/case"
    case.mkdir(parents=True)
    (case / "model-ledger.json").write_text(
        json.dumps(
            {
                "coverage": "partial",
                "responses": [{"model": "system.ai.gpt-6-sol", "workspace": "managed-primary"}],
            }
        )
    )
    with pytest.raises(ValueError, match="unmapped"):
        ledger.finish_lane(tmp_path, lane="smart")


def test_lane_guard_cli_fails_on_cross_model_response(tmp_path):
    case = tmp_path / "artifacts/case"
    case.mkdir(parents=True)
    (case / "model-ledger.json").write_text(
        json.dumps(
            {
                "coverage": "partial",
                "model_coverage": "partial",
                "responses": [{"model": "system.ai.gpt-5-6-sol", "workspace": "managed-primary"}],
            }
        )
    )
    script = Path(ledger.__file__)
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), "--lane", "smart"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 and "Overlapping model" in result.stderr
    assert (tmp_path / "model-ledger.json").is_file()


def test_basic_lane_allows_unknown_workspace_but_rejects_unknown_model_coverage(tmp_path):
    case = tmp_path / "artifacts/case"
    case.mkdir(parents=True)
    file = case / "model-ledger.json"
    response = {"model": None, "reported_model": "system.ai.claude-opus-5-5[1m]", "workspace": None}
    file.write_text(
        json.dumps({"coverage": "unmapped", "model_coverage": "partial", "responses": [response]})
    )
    result = ledger.finish_lane(tmp_path, lane="basic-claude")
    assert result["coverage"] == "unmapped" and result["model_coverage"] == "partial"
    file.write_text(
        json.dumps({"coverage": "unmapped", "model_coverage": "unmapped", "responses": []})
    )
    with pytest.raises(ValueError, match="unmapped"):
        ledger.finish_lane(tmp_path, lane="basic-claude")


def test_lane_ledger_retains_partial_rows_when_one_case_is_unmapped(tmp_path):
    case = tmp_path / "artifacts/case"
    case.mkdir(parents=True)
    (case / "model-ledger.json").write_text(
        json.dumps(
            {
                "coverage": "unmapped",
                "responses": [{"model": None, "workspace": None}],
            }
        )
    )
    with pytest.raises(ValueError, match="unmapped"):
        ledger.finish_lane(tmp_path, lane="smart")
    saved = json.loads((tmp_path / "model-ledger.json").read_text())
    assert saved["coverage"] == "unmapped" and len(saved["responses"]) == 1


def test_lane_ledger_keeps_evidence_when_cross_model_guard_fails(tmp_path):
    case = tmp_path / "artifacts/case"
    case.mkdir(parents=True)
    (case / "model-ledger.json").write_text(
        json.dumps(
            {
                "coverage": "partial",
                "model_coverage": "partial",
                "responses": [{"model": "system.ai.gpt-5-6-sol", "workspace": "managed-primary"}],
            }
        )
    )
    with pytest.raises(ValueError, match="Overlapping model"):
        ledger.finish_lane(tmp_path, lane="smart")
    saved = json.loads((tmp_path / "model-ledger.json").read_text())
    assert saved["coverage"] == "partial" and len(saved["responses"]) == 1


def test_model_guard_rejects_empty_and_unmapped_evidence():
    for events in ([], [{"model": None, "workspace": None}]):
        with pytest.raises(ValueError):
            ledger.check_model_separation(
                events,
                lane="smart",
                desktop_models={"system.ai.gpt-5-6-sol"},
                smart_models={"system.ai.gpt-6-sol"},
            )
