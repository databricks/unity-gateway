"""Ownership decisions bind to complete native HTTP requests, once per prompt."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ucode.smart_routing import ownership
from ucode.smart_routing import ownership_transport as transport


@pytest.fixture
def prepared(tmp_path):
    env = {
        "CLAUDE_CONFIG_DIR": str(tmp_path),
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "1",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1",
    }
    request = {
        "schema_version": 1,
        "policy_version": ownership.POLICY_VERSION,
        "request_id": "root-request",
        "task": {
            "prompt": "Inspect this function.",
            "context": [],
            "context_complete": False,
            "attachments": [],
        },
        "constraints": {
            "root_session": True,
            "subagents_allowed": True,
            "externally_assigned": False,
            "routing_enabled": True,
            "answer_delivery": True,
            "available_tools": ["Agent", "Read"],
        },
        "existing_owners": [],
    }
    result = transport.prepare_request(request, "native-session", env=env)
    state = {
        "schema_version": 1,
        "session_id": "native-session",
        "generation": 1,
        "current": {
            "request_id": request["request_id"],
            "input_sha256": None,
            "policy_version": ownership.POLICY_VERSION,
            "policy_sha256": ownership.policy_sha256(),
            "decision_status": "undecided",
            "ownership": None,
            "dispatch": None,
            "owner_agent_id": None,
            "dispatch_status": "undecided",
            "turn_id": "root-turn",
        },
        "owners": [],
    }
    ownership.write_state("native-session", state, env=env)
    body = {
        "model": "fixture",
        "max_tokens": 1000,
        "system": [{"type": "text", "text": "Caller system contract."}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Project instructions. " + result["instructions"]},
                    {"type": "text", "text": request["task"]["prompt"]},
                ],
            }
        ],
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
    }
    headers = {
        "x-claude-code-request-class": "main",
        "x-claude-code-session-id": "native-session",
        "x-claude-code-prompt-id": "native-prompt",
    }
    return env, body, headers, result["instructions"]


def _capture(prepared, **updates):
    env, body, headers, _ = prepared
    return transport.capture_request(
        updates.get("body", body),
        updates.get("headers", headers),
        workspace="http://fixture.invalid",
        env=env,
        token="fixture-token",
    )


def _service_decision(request):
    return {
        "schema_version": 1,
        "policy_version": ownership.POLICY_VERSION,
        "request_id": request["request_id"],
        "input_sha256": request["input_sha256"],
        "decision_status": "ok",
        "ownership": "main",
        "dispatch": "work_directly",
        "owner_agent_id": None,
        "reason_code": "bounded_lookup",
        "policy_sha256": ownership.policy_sha256(),
        "usage_receipt": None,
    }


@pytest.fixture
def decisions(monkeypatch):
    calls = []

    # Only the remote classifier is simulated; projection, binding and state are real.
    def decide(workspace, request, **kwargs):
        calls.append(copy.deepcopy(request))
        result = _service_decision(request)
        result.update(
            decision_status="ok",
            ownership="main",
            dispatch="work_directly",
            reason_code="bounded_lookup",
        )
        return result

    monkeypatch.setattr(ownership, "decide_ownership", decide)
    return calls


def test_capture_projects_real_context_and_single_flights_retries(prepared, decisions):
    original = copy.deepcopy(prepared[1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _capture(prepared), range(2)))
    assert len(decisions) == 1
    request = decisions[0]
    assert request["task"]["context_complete"]
    context = json.dumps(request["task"]["context"])
    assert "Caller system contract" in context and "Project instructions" in context
    assert "tool_definition" in context and prepared[3] not in context
    assert responses[0] == responses[1]
    assert prepared[1] == original
    assert responses[0]["model"] == original["model"]
    assert responses[0]["tools"] == original["tools"]
    state = ownership.read_state("native-session", env=prepared[0])
    assert state["current"]["input_sha256"] == request["input_sha256"]
    assert state["current"]["turn_id"] == "root-turn"
    assert state["current"]["dispatch_status"] == "started"


def test_foreign_prompt_requires_native_continuation_permit(prepared, decisions):
    _capture(prepared)
    headers = {**prepared[2], "x-claude-code-prompt-id": "notification-prompt"}
    with pytest.raises(ownership.OwnershipStateError, match="foreign prompt"):
        _capture(prepared, headers=headers)
    state = ownership.read_state("native-session", env=prepared[0])
    state["current"]["continuation_turn_id"] = "notification-turn"
    state["generation"] += 1
    ownership.write_state("native-session", state, env=prepared[0])
    _capture(prepared, headers=headers)
    _capture(prepared, headers=headers)
    with pytest.raises(ownership.OwnershipStateError, match="foreign prompt"):
        _capture(prepared, headers={**headers, "x-claude-code-prompt-id": "other-prompt"})
    assert len(decisions) == 1


def test_compacted_request_restores_instructions_without_reclassification(prepared, decisions):
    _capture(prepared)
    compacted = {**prepared[1], "messages": [{"role": "user", "content": "Compacted task."}]}
    restored = _capture(prepared, body=compacted)
    rendered = " ".join(json.dumps(restored).replace("\\n", " ").split())
    assert "claude_answer_delivery: AVAILABLE" in rendered
    for fragment in (
        "Use one owner for a connected outcome.",
        "does not retain a duplicate investigation.",
        "Launch ready independent assignments together.",
        "Reuse complete instructions already received.",
        "| Role | Scope | Claude default |",
        "Before acceptance, every caller-required evidence item must be cited.",
        "ask the same owner for the complete corrected answer",
    ):
        assert rendered.count(fragment) == 1, fragment
    assert (
        "Do not investigate the request in the parent in order to decide whether to delegate it."
        not in rendered
    )
    assert _capture(prepared, body=restored) == restored
    assert len(decisions) == 1


@pytest.mark.parametrize("request_class", ["subagent", "compaction", "auxiliary"])
def test_internal_requests_do_not_classify(prepared, decisions, request_class):
    assert _capture(prepared, headers={"x-claude-code-request-class": request_class}) is prepared[1]
    assert not decisions


def test_disabled_routing_does_not_require_identity_or_call_service(prepared, decisions):
    prepared[0]["ENABLE_SMART_ROUTER_ORCHESTRATOR"] = "0"
    assert _capture(prepared, headers={}) is prepared[1]
    assert not decisions


def test_missing_or_foreign_identity_cannot_dispatch(prepared, decisions):
    with pytest.raises(ownership.OwnershipProtocolError, match="headers"):
        _capture(prepared, headers={})
    with pytest.raises(ownership.OwnershipStateError, match="prepared ownership intent"):
        _capture(prepared, headers={**prepared[2], "x-claude-code-session-id": "foreign"})
    body = {**prepared[1], "messages": [{"role": "user", "content": "No marker."}]}
    with pytest.raises(ownership.OwnershipStateError, match="fresh ownership marker"):
        _capture(prepared, body=body)
    assert not decisions


def test_ambiguous_classifier_failure_does_not_silently_charge_again(prepared, monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise ownership.OwnershipProtocolError("charged invalid response")

    monkeypatch.setattr(ownership, "decide_ownership", fail)
    with pytest.raises(ownership.OwnershipProtocolError, match="charged invalid"):
        _capture(prepared)
    with pytest.raises(ownership.OwnershipProtocolError, match="duplicate charge"):
        _capture(prepared)
    assert len(calls) == 1


def test_unreadable_content_is_an_error_without_credentials_or_network(prepared, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unreadable request must not make a model call")

    monkeypatch.setattr(ownership, "get_databricks_token", forbidden)
    monkeypatch.setattr(ownership.urllib.request, "build_opener", forbidden)
    body = copy.deepcopy(prepared[1])
    body["messages"][0]["content"].append({"type": "image", "source": {"type": "url", "url": "x"}})
    with pytest.raises(ownership.OwnershipServiceError, match="cannot classify attachments"):
        _capture(prepared, body=body)
    state = ownership.read_state("native-session", env=prepared[0])
    assert state["current"]["dispatch_status"] == "undecided"


def test_new_user_intent_reclassifies_even_when_text_is_identical(prepared, decisions):
    _capture(prepared)
    request = copy.deepcopy(decisions[0])
    request.pop("input_sha256")
    request["request_id"] = "next-root-request"
    result = transport.prepare_request(request, "native-session", env=prepared[0])
    state = ownership.read_state("native-session", env=prepared[0])
    state["current"].update(
        request_id=request["request_id"],
        input_sha256=None,
        decision_status="undecided",
        ownership=None,
        dispatch=None,
        owner_agent_id=None,
        dispatch_status="undecided",
        turn_id="next-root-turn",
    )
    state["generation"] += 1
    ownership.write_state("native-session", state, env=prepared[0])
    body = copy.deepcopy(prepared[1])
    body["messages"][0]["content"][0]["text"] = result["instructions"]
    _capture(
        prepared,
        body=body,
        headers={**prepared[2], "x-claude-code-prompt-id": "next-native-prompt"},
    )
    assert len(decisions) == 2
    assert decisions[1]["request_id"] == "next-root-request"


@pytest.mark.parametrize("field,value", [("input_sha256", "0" * 64), ("request_id", "foreign")])
def test_cached_decision_identity_is_revalidated(prepared, decisions, field, value):
    _capture(prepared)
    path = transport._intent_path("native-session", "root-request", prepared[0])
    intent = json.loads(path.read_text())
    intent["decision"][field] = value
    path.write_text(json.dumps(intent))
    with pytest.raises(ownership.OwnershipProtocolError, match="does not match"):
        _capture(prepared)
    assert len(decisions) == 1


@pytest.mark.parametrize("transition", ["failed", "missing", "changed_scope", "completed"])
def test_owner_freshness_is_checked_under_install_lock(prepared, monkeypatch, transition):
    env = prepared[0]
    state = ownership.read_state("native-session", env=env)
    state["owners"] = [
        {"agent_id": "actual-child", "scope": "Inspect function", "state": "running"}
    ]
    state["generation"] += 1
    ownership.write_state("native-session", state, env=env)
    calls = []

    def decide(workspace, request, **kwargs):
        calls.append(request)
        current = ownership.read_state("native-session", env=env)
        if transition == "missing":
            current["owners"] = []
        elif transition == "changed_scope":
            current["owners"][0]["scope"] = "Different task"
        else:
            current["owners"][0]["state"] = transition
        current["generation"] += 1
        ownership.write_state("native-session", current, env=env)
        result = _service_decision(request)
        result.update(
            decision_status="ok",
            ownership="subagent",
            dispatch="reuse_subagent",
            owner_agent_id="actual-child",
            reason_code="continue_existing_owner",
        )
        return result

    monkeypatch.setattr(ownership, "decide_ownership", decide)
    if transition == "completed":
        assert prepared[3] not in json.dumps(_capture(prepared))
        assert (
            ownership.read_state("native-session", env=env)["current"]["dispatch_status"]
            == "awaiting_dispatch"
        )
    else:
        for _ in range(2):
            with pytest.raises(ownership.OwnershipProtocolError, match="fresh same-session"):
                _capture(prepared)
        assert (
            ownership.read_state("native-session", env=env)["current"]["dispatch_status"]
            == "undecided"
        )
    assert len(calls) == 1
