"""Component coverage for displayed routing reasons and unchanged router decisions."""

import io
import json

import pytest

from ucode.smart_routing import claude_routing, codex_routing, routing
from ucode.smart_routing.rationale import format_rationale


@pytest.fixture
def router_response():
    return {
        "route_selection": [
            {
                "route_option": {"model": "gpt-5-6-luna", "harness": "codex"},
                "params": {
                    "router_version": "task_v3",
                    "task_tier": "Low",
                    "served_tier": "Low",
                    "profiler_status": "ok",
                    "evidence_spans": ["Inspect the parser."],
                    "task_profile": {
                        "task_type": "investigate",
                        "localization_burden": "explicit_target",
                        "scope": "localized",
                        "reasoning_depth": "direct",
                        "environment_burden": "workspace",
                        "verification_strength": "explicit",
                        "difficulty": "low",
                    },
                },
            }
        ],
        "rationale": (
            "Routed to Low because its task-only profile is low: "
            "an explicit target, localized scope, direct reasoning. "
            "Request: “Inspect the parser.”."
        ),
    }


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({}, "This task has a clear target and limited scope."),
        ({"scope": "connected"}, "This task involves several related components."),
        ({"scope": "cross_cutting"}, "This task spans several components."),
        (
            {"localization_burden": "open_search"},
            "This task needs investigation to find what needs attention.",
        ),
        ({"environment_burden": "live_external"}, "This task depends on live systems or data."),
        ({"reasoning_depth": "multi_step"}, "This task involves several dependent steps."),
    ],
)
def test_reasons_describe_the_task_profile(router_response, profile, expected):
    selection = router_response["route_selection"][0]
    selection["params"]["task_profile"].update(profile)

    assert format_rationale(selection, router_response["rationale"]) == expected


def test_availability_explanation_and_custom_gateway_notice(router_response):
    selection = router_response["route_selection"][0]
    selection["params"]["served_tier"] = "High"
    rationale = (
        router_response["rationale"]
        + " Your configured model availability requires service at High."
        + " Contact the platform team with feedback."
    )

    assert format_rationale(selection, rationale) == (
        "This task has a clear target and limited scope. "
        "A higher-tier model was selected because the requested tier isn't available. "
        "Contact the platform team with feedback."
    )


def test_unavailable_profile_explains_the_fallback(router_response):
    selection = router_response["route_selection"][0]
    selection["params"].update(
        task_tier="High", served_tier="High", profiler_status="unavailable", task_profile=None
    )

    assert format_rationale(selection, router_response["rationale"]) == (
        "The task could not be assessed, so a high-capability model was selected."
    )


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        {"router_version": "task_v4"},
        {"task_profile": None},
        {"task_profile": {"scope": "unknown"}},
        {"evidence_spans": []},
        {"served_tier": "unknown"},
    ],
)
def test_unrecognized_metadata_keeps_the_gateway_reason(router_response, params):
    selection = router_response["route_selection"][0]
    if isinstance(params, dict):
        selection["params"].update(params)
    else:
        selection["params"] = params

    assert format_rationale(selection, router_response["rationale"]) == router_response["rationale"]


@pytest.mark.parametrize("rationale", ["", "A custom routing explanation."])
def test_unrecognized_text_is_preserved(router_response, rationale):
    assert format_rationale(router_response["route_selection"][0], rationale) == rationale


@pytest.mark.parametrize(
    ("client", "model", "models"),
    [
        (
            claude_routing,
            "claude-sonnet-5",
            ["system.ai.claude-sonnet-5", "system.ai.claude-opus-4-8"],
        ),
        (codex_routing, "gpt-5-6-luna", ["system.ai.gpt-5-6-luna", "system.ai.gpt-5-6-sol"]),
    ],
)
def test_agent_notices_preserve_selection_and_diagnostics(
    router_response, monkeypatch, tmp_path, client, model, models
):
    router_response["route_selection"][0]["route_option"]["model"] = model
    requests = []

    # Replace only the remote response; exercise parsing, notices, and diagnostic writes.
    def urlopen(request, timeout):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps(router_response).encode())

    monkeypatch.setattr(routing.urllib.request, "urlopen", urlopen)
    decision, error = client.request_routing_decision(
        "https://example.databricks.com", "token", "Inspect the parser.", models
    )

    assert error is None
    assert decision.model == f"system.ai.{model}"
    assert decision.raw_model == model
    assert decision.rationale == "This task has a clear target and limited scope."
    assert len(requests) == 1
    assert requests[0]["task"] == {"prompt": "Inspect the parser."}
    assert requests[0]["route_selector"] == {"router_name": "task_v3"}
    for is_subagent in (False, True):
        assert f"Reason : {decision.rationale}" in decision.display_message(is_subagent=is_subagent)

    decisions_path = tmp_path / "decisions.jsonl"
    routing.write_decision_record(
        decisions_path, {}, "Inspect the parser.", decision, decision.model
    )
    record = json.loads(decisions_path.read_text())
    assert record["rationale"] == router_response["rationale"]
    assert record["requested_model"] == decision.model
