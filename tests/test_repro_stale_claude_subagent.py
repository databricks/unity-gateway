from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import repro_stale_claude_subagent as repro


def completed_events():
    events = [{"type": "system", "subtype": "init", "agents": [repro.ROUTE_AGENT]}]
    for index in range(4):
        tool_id = f"call-{index}"
        events.extend(
            [
                {
                    "type": "assistant",
                    "message": {
                        "id": "first-message",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "id": tool_id,
                                "input": {"subagent_type": "Explore"},
                            }
                        ],
                    },
                },
                {
                    "type": "system",
                    "subtype": "task_started",
                    "tool_use_id": tool_id,
                    "subagent_type": repro.ROUTE_AGENT,
                    "spawn_depth": 1,
                },
                {
                    "type": "system",
                    "subtype": "task_notification",
                    "tool_use_id": tool_id,
                    "status": "completed",
                },
            ]
        )
    events.append({"type": "result", "subtype": "success", "is_error": False})
    return events


def classify(events, returncode=0):
    return repro._classify(
        "\n".join(json.dumps(event) for event in events), returncode, Path("claude.log")
    )


def test_classifies_completed_routed_children_without_claiming_a_fix():
    result = classify(completed_events())
    assert result.outcome == "no_failure_observed"
    assert result.registration_evidence == "present_at_first_init"
    assert result.routed_starts == result.completions == 4


@pytest.mark.parametrize("missing_subtype", ["task_started", "task_notification", "init"])
def test_four_tool_calls_alone_are_insufficient(missing_subtype):
    events = [event for event in completed_events() if event.get("subtype") != missing_subtype]
    assert classify(events).outcome == "inconclusive"


def test_sequential_batches_are_not_parallel_first_use():
    events = completed_events()
    for index, event in enumerate(events):
        if event["type"] == "assistant":
            event["message"]["id"] = f"message-{index}"
    assert classify(events).outcome == "inconclusive"


def test_nested_calls_do_not_count_as_top_level():
    events = completed_events()
    for event in events:
        if event["type"] == "assistant":
            event["parent_tool_use_id"] = "parent"
    assert classify(events).outcome == "inconclusive"


def test_unrouted_explore_completion_is_not_a_success():
    events = completed_events()
    for event in events:
        if event.get("subtype") == "task_started":
            event["subagent_type"] = "Explore"
    assert classify(events).outcome == "inconclusive"


def test_assistant_claim_of_missing_agent_is_not_registry_failure():
    events = completed_events()
    events.append(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": f"Agent type '{repro.ROUTE_AGENT}' not found",
                    }
                ]
            },
        }
    )
    assert classify(events).outcome == "no_failure_observed"


@pytest.mark.parametrize("registered", [True, False])
def test_actual_tool_error_records_initial_registration(registered):
    events = completed_events()
    events[0]["agents"] = [repro.ROUTE_AGENT] if registered else []
    events.append(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-0",
                        "is_error": True,
                        "content": f"Agent type '{repro.ROUTE_AGENT}' not found. Available agents: Explore",
                    }
                ]
            },
        }
    )
    result = classify(events)
    assert result.outcome == "registry_failure"
    assert result.registration_evidence == (
        "present_at_first_init" if registered else "absent_at_first_init"
    )


@pytest.mark.parametrize("returncode", [None, 1, 124])
def test_unknown_or_failed_exit_is_inconclusive(returncode):
    assert classify(completed_events(), returncode).outcome == "inconclusive"


def test_failed_completion_is_not_success():
    events = completed_events()
    events[3]["status"] = "failed"
    events[3]["summary"] = "Authentication failed"
    assert classify(events).outcome == "inconclusive"


def reload_evidence(tmp_path, *, missing_after=False, complete_before=True, reload=True):
    events = []
    debug = []
    for phase in ("before", "after"):
        if phase == "after" and reload:
            events.extend(
                [
                    {
                        "type": "user",
                        "message": {"content": "<command-name>/reload-plugins</command-name>"},
                    },
                    {
                        "type": "system",
                        "subtype": "local_command",
                        "content": "<local-command-stdout>Reloaded: 6 agents</local-command-stdout>",
                    },
                ]
            )
            debug.append("refreshActivePlugins: 6 agents")
        marker = f"{phase.upper()}_RELOAD"
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Agent",
                            "id": phase,
                            "input": {"subagent_type": "Explore", "prompt": f"Reply with {marker}"},
                        }
                    ]
                },
            }
        )
        if phase == "after" and missing_after:
            events.append(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": phase,
                                "is_error": True,
                                "content": f"Agent type '{repro.ROUTE_AGENT}' not found",
                            }
                        ]
                    },
                }
            )
            continue
        events.append(
            {
                "type": "user",
                "toolUseResult": {"agentId": f"child-{phase}"},
                "message": {"content": [{"type": "tool_result", "tool_use_id": phase}]},
            }
        )
        if phase == "before" and not complete_before:
            continue
        debug.append(f"agentId=child-{phase} agentType={repro.ROUTE_AGENT} exitPath=completed")
        events.append(
            {
                "type": "user",
                "message": {
                    "content": (
                        f"<task-notification><tool-use-id>{phase}</tool-use-id>"
                        f"<status>completed</status><result>{marker}</result></task-notification>"
                    )
                },
            }
        )
    projects = tmp_path / "claude-config/projects/project"
    projects.mkdir(parents=True)
    (projects / "session.jsonl").write_text("\n".join(json.dumps(event) for event in events))
    (tmp_path / "debug.log").write_text("\n".join(debug))


def test_reload_requires_success_before_actual_missing_agent_error(tmp_path):
    reload_evidence(tmp_path, missing_after=True)
    result = repro._classify_reload(tmp_path, repro.ROUTE_AGENT, 0)
    assert result.outcome == "registry_failure"
    assert result.registration_evidence == "proved_before_refresh"
    assert result.completions == 1


def test_reload_survival_requires_two_routed_completions(tmp_path):
    reload_evidence(tmp_path)
    result = repro._classify_reload(tmp_path, repro.ROUTE_AGENT, 0)
    assert result.outcome == "no_failure_observed"
    assert result.completions == 2


@pytest.mark.parametrize("missing_after", [False, True])
def test_missing_before_completion_cannot_prove_registration_loss(tmp_path, missing_after):
    reload_evidence(tmp_path, missing_after=missing_after, complete_before=False)
    assert repro._classify_reload(tmp_path, repro.ROUTE_AGENT, 0).outcome == "inconclusive"


def test_no_reload_cannot_prove_refresh_survival(tmp_path):
    reload_evidence(tmp_path, reload=False)
    assert repro._classify_reload(tmp_path, repro.ROUTE_AGENT, 0).outcome == "inconclusive"


def test_builtin_children_cannot_prove_routed_agent_survival(tmp_path):
    reload_evidence(tmp_path)
    debug_path = tmp_path / "debug.log"
    debug_path.write_text(debug_path.read_text().replace(repro.ROUTE_AGENT, "Explore"))
    assert repro._classify_reload(tmp_path, repro.ROUTE_AGENT, 0).outcome == "inconclusive"
