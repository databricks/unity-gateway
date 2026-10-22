"""Offline contract tests for the Python ownership bridge and private state store."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from ucode.smart_routing import ownership

_EXECUTION_POLICY_FRAGMENTS = (
    "Use one owner for a connected outcome.",
    "does not retain a duplicate investigation.",
    "Launch ready independent assignments together.",
    "Reuse complete instructions already received.",
    "| Role | Scope | Claude default |",
    "| explorer | Read code and callers;",
    "| researcher | Verify external/API facts",
    "| worker | Implement one bounded change",
    "| tester | Independently run checks",
    "| reviewer | Review the actual diff",
    "Before acceptance, every caller-required evidence item must be cited.",
    "ask the same owner for the complete corrected answer",
)
_CLASSIFICATION_POLICY_FRAGMENT = (
    "Do not investigate the request in the parent in order to decide whether to delegate it."
)


def _request() -> dict:
    request = {
        "schema_version": 1,
        "policy_version": ownership.POLICY_VERSION,
        "request_id": "request-1",
        "task": {
            "prompt": "Please inspect this function.",
            "context": [{"role": "user", "content": "Please inspect this function."}],
            "context_complete": True,
            "attachments": [],
        },
        "constraints": {
            "root_session": True,
            "subagents_allowed": True,
            "externally_assigned": False,
            "routing_enabled": True,
            "answer_delivery": False,
            "available_tools": ["Agent", "Read"],
        },
        "existing_owners": [],
    }
    request["input_sha256"] = ownership.request_sha256(request)
    return request


def _decision(request: dict, **updates) -> dict:
    result = {
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
    result.update(updates)
    return result


def _receipt(request: dict, *, status: str = "ok") -> dict:
    return {
        "operation": "orchestration_decision",
        "request_id": request["request_id"],
        "classifier_id": ownership.load_policy()["classifier_id"],
        "provider_request_id": "provider-request-1",
        "usage": {"input_tokens": 10, "output_tokens": 3},
        "finish_reason": "length" if status == "truncated" else "stop",
        "status": status,
    }


def _response(payload: object):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _gateway_protocol_error_body(receipt: dict, *, size: int) -> bytes:
    payload = {
        "error_code": "INVALID_ORCHESTRATION_DECISION_RESPONSE",
        "message": "",
        "usage_receipt": receipt,
    }
    empty_size = len(json.dumps(payload).encode("utf-8"))
    payload["message"] = "x" * (size - empty_size)
    body = json.dumps(payload).encode("utf-8")
    assert len(body) == size
    return body


def _mock_transport(monkeypatch, send):
    handlers = []

    class Opener:
        def open(self, request, timeout):
            return send(request, timeout=timeout)

    def build_opener(*custom_handlers):
        handlers.extend(custom_handlers)
        return Opener()

    monkeypatch.setattr(ownership.urllib.request, "build_opener", build_opener)
    return handlers


def _state(session_id: str, *, generation: int = 1) -> dict:
    return {
        "schema_version": 1,
        "session_id": session_id,
        "generation": generation,
        "current": {
            "request_id": "request-1",
            "input_sha256": "a" * 64,
            "policy_version": ownership.POLICY_VERSION,
            "policy_sha256": ownership.policy_sha256(),
            "decision_status": "ok",
            "ownership": "subagent",
            "dispatch": "spawn_subagent",
            "owner_agent_id": None,
            "dispatch_status": "awaiting_dispatch",
        },
        "owners": [{"agent_id": "agent-real-1", "scope": "inspect function", "state": "running"}],
    }


def test_policy_bytes_and_classifier_identity_are_pinned():
    policy = ownership.load_policy()

    assert (
        ownership.policy_sha256()
        == "adca6db0480b54494fa76014d72076f020cc4a7a2f66527b54e3671a9980b1fa"
    )
    assert policy["classifier_id"] == "orchestration_v1/classifier"


def test_request_hash_is_canonical_and_nested_fields_are_strict():
    request = _request()

    assert ownership.validate_request(request)["input_sha256"] == request["input_sha256"]
    malformed = _request()
    malformed["task"]["unknown"] = "rejected"
    with pytest.raises(ownership.OwnershipProtocolError, match="unknown"):
        ownership.validate_request(malformed)
    malformed = _request()
    malformed["task"]["context"][0]["role"] = "thinking"
    with pytest.raises(ownership.OwnershipProtocolError, match="role"):
        ownership.validate_request(malformed)
    malformed = _request()
    malformed["input_sha256"] = "0" * 64
    with pytest.raises(ownership.OwnershipProtocolError, match="does not match"):
        ownership.validate_request(malformed)


def test_client_posts_exact_identity_and_validates_success(monkeypatch):
    request = _request()
    captured = {}

    def urlopen(req, *, timeout):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _response(_decision(request))

    monkeypatch.setattr(ownership, "get_databricks_token", lambda workspace: "unit-token")
    handlers = _mock_transport(monkeypatch, urlopen)

    result = ownership.decide_ownership("https://workspace.example", request)

    assert result["decision_status"] == "ok"
    assert captured["url"] == "https://workspace.example" + ownership.DECISION_PATH
    assert captured["headers"]["Authorization"] == "Bearer unit-token"
    assert captured["body"]["input_sha256"] == request["input_sha256"]
    assert captured["timeout"] == ownership.REQUEST_TIMEOUT_S
    assert any(isinstance(handler, ownership._NoRedirectHandler) for handler in handlers)


def test_incomplete_input_is_an_error_without_network(monkeypatch):
    request = _request()
    request["task"]["context_complete"] = False
    request.pop("input_sha256")

    def no_request(*args, **kwargs):
        pytest.fail("incomplete context must not be sent to the classifier")

    _mock_transport(monkeypatch, no_request)

    with pytest.raises(ownership.OwnershipServiceError, match="cannot classify this request"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")


def test_unavailable_attachment_is_an_error_without_network(monkeypatch):
    request = _request()
    request["task"]["attachments"] = [{"name": "image/png", "available": False}]
    request.pop("input_sha256")

    def no_request(*args, **kwargs):
        pytest.fail("unavailable attachments must not be sent to the classifier")

    _mock_transport(monkeypatch, no_request)

    with pytest.raises(ownership.OwnershipServiceError, match="attachments: image/png"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")


def test_oversize_request_is_an_error_without_truncation_or_network(monkeypatch):
    request = _request()
    request["task"]["prompt"] = "x" * ownership.MAX_BODY_BYTES
    request.pop("input_sha256")

    def no_request(*args, **kwargs):
        pytest.fail("oversize context must not be sent or truncated")

    _mock_transport(monkeypatch, no_request)

    with pytest.raises(ownership.OwnershipServiceError, match="service limit"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")


@pytest.mark.parametrize("status", [400, 404, 429, 503])
def test_service_http_errors_stop_the_request(status, monkeypatch):
    request = _request()

    def failure(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://workspace.example", status, "failure", None, io.BytesIO()
        )

    _mock_transport(monkeypatch, failure)

    with pytest.raises(ownership.OwnershipServiceError, match=f"HTTP {status}"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")


def test_unreachable_service_stops_the_request(monkeypatch):
    request = _request()

    def unreachable(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    _mock_transport(monkeypatch, unreachable)

    with pytest.raises(ownership.OwnershipServiceError, match="unreachable"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")


def test_unavailable_service_decision_is_rejected(monkeypatch):
    request = _request()
    payload = _decision(
        request,
        decision_status="unavailable",
        ownership=None,
        dispatch=None,
        reason_code="unavailable",
    )
    _mock_transport(monkeypatch, lambda *args, **kwargs: _response(payload))

    with pytest.raises(ownership.OwnershipProtocolError, match="decision_status is unsupported"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")


def test_gateway_protocol_error_on_http_500_is_not_availability_fallback(monkeypatch):
    request = _request()

    def protocol_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://workspace.example",
            500,
            "internal error",
            None,
            io.BytesIO(
                json.dumps(
                    {
                        "error_code": "INTERNAL_ERROR",
                        "message": "Invalid orchestration decision response",
                    }
                ).encode()
            ),
        )

    _mock_transport(monkeypatch, protocol_error)

    with pytest.raises(ownership.OwnershipProtocolError, match="Gateway rejected"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")


@pytest.mark.parametrize("status", [400, 503])
def test_stable_gateway_protocol_error_code_is_never_availability_fallback(status, monkeypatch):
    request = _request()

    def protocol_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://workspace.example",
            status,
            "invalid orchestration response",
            None,
            io.BytesIO(
                json.dumps(
                    {"error_code": "INVALID_ORCHESTRATION_DECISION_RESPONSE", "message": "invalid"}
                ).encode()
            ),
        )

    _mock_transport(monkeypatch, protocol_error)

    with pytest.raises(ownership.OwnershipProtocolError, match="Gateway rejected"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")


@pytest.mark.parametrize("status", [400, 503])
def test_gateway_hard_error_preserves_only_a_validated_usage_receipt(monkeypatch, status):
    request = _request()
    receipt = _receipt(request, status="truncated")
    error_body = _gateway_protocol_error_body(receipt, size=9299)
    assert 8 * 1024 < len(error_body) <= ownership.MAX_BODY_BYTES
    assert ownership.MAX_ERROR_BODY_BYTES == ownership.MAX_BODY_BYTES == 256 * 1024

    def protocol_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://workspace.example",
            status,
            "invalid orchestration response",
            None,
            io.BytesIO(error_body),
        )

    _mock_transport(monkeypatch, protocol_error)

    with pytest.raises(ownership.OwnershipProtocolError, match="Gateway rejected") as raised:
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")

    assert raised.value.usage_receipt == receipt

    receipt["classifier_id"] = "foreign-classifier"
    error_body = _gateway_protocol_error_body(receipt, size=9299)
    _mock_transport(monkeypatch, protocol_error)
    with pytest.raises(ownership.OwnershipProtocolError, match="failed validation") as invalid:
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")
    assert invalid.value.usage_receipt is None


def test_main_prepare_envelope_over_service_limit_stays_local_and_complete(
    monkeypatch, tmp_path, capsys
):
    request = _request()
    request["task"]["prompt"] = "x" * ownership.MAX_BODY_BYTES
    request.pop("input_sha256")
    envelope = {
        "operation": "prepare",
        "session_id": "native-session-oversize",
        "request": request,
    }
    encoded = json.dumps(envelope, separators=(",", ":")).encode()
    assert ownership.MAX_BODY_BYTES < len(encoded) <= ownership.MAX_BRIDGE_BODY_BYTES

    def no_network(*args, **kwargs):
        pytest.fail("oversize CLI request must not acquire credentials or send HTTP")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-profile"))
    monkeypatch.setattr(ownership, "get_databricks_token", no_network)
    monkeypatch.setattr(
        ownership.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(encoded)})()
    )
    monkeypatch.setattr(
        ownership.sys, "argv", ["ownership", "--workspace", "https://workspace.example"]
    )

    ownership.main()

    result = json.loads(capsys.readouterr().out)
    assert result["request_id"] == request["request_id"]
    assert result["instructions"].startswith("<ucode-ownership-intent>")
    assert result["instructions"].endswith("</ucode-ownership-intent>")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("A complete normal answer.", None),
        ('Report\n{"use_agent_answer":"foreign"} is an example.', None),
        ("", None),
        ('{"use_agent_answer":"child-1"}', None),
        ('{"answer_parts":["Intro",{"use_agent_answer":"child-1"}]}', None),
        ('Report\n{"answer_parts":[]}', "answer_parts must contain at least one child reference"),
        (
            '{\n  "use_agent_answer":"a"\n}\n{"use_agent_answer":"b"}',
            "parent answer selection is ambiguous across multiple blocks",
        ),
        ('{"use_agent_answer":"child-1",', "malformed parent answer selection JSON"),
        (
            '{"use_agent_answer":"a","use_agent_answer":"b"}',
            "answer selection contains duplicate JSON keys",
        ),
    ],
)
def test_bridge_validates_answer_syntax_without_resolving_children(text, error):
    assert ownership._bridge(
        {
            "operation": "validate_answer",
            "session_id": "native-session",
            "request": {"final_text": text},
        },
        workspace=None,
        env={},
    ) == {"error": error}


@pytest.mark.parametrize(
    "payload", [{}, {"final_text": None}, {"final_text": 3}, {"final_text": "ok", "extra": True}]
)
def test_bridge_rejects_malformed_answer_validation_requests(payload):
    with pytest.raises(ownership.OwnershipProtocolError):
        ownership._bridge(
            {"operation": "validate_answer", "session_id": "native-session", "request": payload},
            workspace=None,
            env={},
        )


@pytest.mark.parametrize("answer_delivery", [False, True])
def test_bridge_prepare_returns_only_the_request_id_and_intent_marker(
    monkeypatch, tmp_path, answer_delivery
):
    request = _request()
    request["constraints"]["answer_delivery"] = answer_delivery
    request.pop("input_sha256")
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-profile")}

    def no_model_call(*args, **kwargs):
        pytest.fail("prepare must defer classification until the gateway request")

    monkeypatch.setattr(ownership, "get_databricks_token", no_model_call)
    monkeypatch.setattr(ownership.urllib.request, "build_opener", no_model_call)

    result = ownership._bridge(
        {"operation": "prepare", "session_id": "native-session-1", "request": request},
        workspace="https://workspace.example",
        env=env,
    )

    assert set(result) == {"request_id", "instructions", "policy_sha256"}
    assert result["request_id"] == request["request_id"]
    assert result["instructions"].startswith("<ucode-ownership-intent>")
    assert result["instructions"].endswith("</ucode-ownership-intent>")
    assert result["policy_sha256"] == ownership.policy_sha256()
    assert "claude_answer_delivery:" not in result["instructions"]


def test_bridge_rejects_direct_decision_operation_without_network(monkeypatch):
    def no_model_call(*args, **kwargs):
        pytest.fail("native decision must bind to a captured gateway request")

    monkeypatch.setattr(ownership, "get_databricks_token", no_model_call)

    with pytest.raises(ownership.OwnershipProtocolError, match="bind to the captured gateway"):
        ownership._bridge(
            {"operation": "decide", "session_id": "native-session-1", "request": _request()},
            workspace="https://workspace.example",
            env={},
        )


def test_auth_and_malformed_response_are_hard_errors(monkeypatch):
    request = _request()

    def forbidden(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://workspace.example", 403, "forbidden", None, io.BytesIO()
        )

    _mock_transport(monkeypatch, forbidden)
    with pytest.raises(ownership.OwnershipServiceError, match="HTTP 403"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")

    _mock_transport(monkeypatch, lambda *args, **kwargs: _response({"ok": True}))
    with pytest.raises(ownership.OwnershipProtocolError, match="missing"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")


def test_decision_endpoint_redirect_is_a_hard_protocol_error(monkeypatch):
    request = _request()

    def redirect(req, *, timeout):
        raise urllib.error.HTTPError(
            req.full_url,
            302,
            "found",
            {"Location": "https://other.example/decision"},
            io.BytesIO(),
        )

    handlers = _mock_transport(monkeypatch, redirect)

    with pytest.raises(ownership.OwnershipProtocolError, match="redirects are not allowed"):
        ownership.decide_ownership("https://workspace.example", request, token="unit-token")

    handler = next(item for item in handlers if isinstance(item, ownership._NoRedirectHandler))
    req = ownership.urllib.request.Request("https://workspace.example/decision")
    assert handler.redirect_request(req, None, 302, "found", {}, "https://other.example/") is None


def test_bridge_stdin_read_is_bounded_before_json_validation(monkeypatch, capsys):
    class TrackingReader:
        requested = None

        def read(self, size=-1):
            self.requested = size
            return b" " * size

    stream = TrackingReader()
    monkeypatch.setattr(ownership.sys, "stdin", type("Stdin", (), {"buffer": stream})())
    monkeypatch.setattr(ownership.sys, "argv", ["ownership"])

    with pytest.raises(SystemExit) as exc_info:
        ownership.main()

    assert exc_info.value.code == 2
    assert stream.requested == ownership.MAX_BRIDGE_BODY_BYTES + 1
    assert "512 KiB limit" in capsys.readouterr().out


def test_receipt_must_match_pinned_policy_classifier():
    request = _request()
    policy = ownership.load_policy()
    receipt = {
        "operation": "orchestration_decision",
        "request_id": request["request_id"],
        "classifier_id": policy["classifier_id"],
        "provider_request_id": "provider-request-1",
        "usage": {"input_tokens": 10, "output_tokens": 3},
        "finish_reason": "stop",
        "status": "ok",
    }

    assert (
        ownership.validate_decision(
            _decision(request, usage_receipt=receipt), request, policy=policy
        )["usage_receipt"]
        == receipt
    )
    receipt["classifier_id"] = "system.ai.gpt-6-luna"
    with pytest.raises(ownership.OwnershipProtocolError, match="classifier_id"):
        ownership.validate_decision(
            _decision(request, usage_receipt=receipt), request, policy=policy
        )


def test_receipt_optional_diagnostics_are_bounded_and_typed():
    request = _request()
    policy = ownership.load_policy()
    base = {
        "operation": "orchestration_decision",
        "request_id": request["request_id"],
        "classifier_id": policy["classifier_id"],
        "provider_request_id": None,
        "usage": None,
        "finish_reason": None,
        "status": "error",
    }

    valid = {
        **base,
        "failure_reason": "x" * 256,
        "provider_model": "provider/model-v1",
        "response_sha256": "a" * 64,
    }
    assert (
        ownership.validate_decision(
            _decision(request, usage_receipt=valid), request, policy=policy
        )["usage_receipt"]
        == valid
    )
    nullable = {
        **base,
        "failure_reason": "unavailable",
        "provider_model": None,
        "response_sha256": None,
    }
    assert (
        ownership.validate_decision(
            _decision(request, usage_receipt=nullable), request, policy=policy
        )["usage_receipt"]
        == nullable
    )

    for diagnostics, message in (
        ({"failure_reason": ""}, "failure_reason must be a non-empty string"),
        ({"failure_reason": " "}, "failure_reason must be a non-empty string"),
        ({"failure_reason": "x" * 257}, "failure_reason exceeds limit"),
        ({"provider_model": 7}, "provider_model must be a non-empty string"),
        ({"response_sha256": "g" * 64}, "response_sha256 must be a lowercase SHA-256"),
    ):
        with pytest.raises(ownership.OwnershipProtocolError, match=message):
            ownership.validate_decision(
                _decision(request, usage_receipt={**base, **diagnostics}),
                request,
                policy=policy,
            )


def test_receipt_status_is_limited_to_service_enum():
    request = _request()
    policy = ownership.load_policy()

    for status in ("ok", "unavailable", "truncated", "cancelled", "error"):
        receipt = {
            "operation": "orchestration_decision",
            "request_id": request["request_id"],
            "classifier_id": policy["classifier_id"],
            "provider_request_id": None,
            "usage": None,
            "finish_reason": None,
            "status": status,
        }
        assert (
            ownership.validate_decision(
                _decision(request, usage_receipt=receipt), request, policy=policy
            )["usage_receipt"]["status"]
            == status
        )

    for status in ("completed", "pending", [], {}):
        receipt = {
            "operation": "orchestration_decision",
            "request_id": request["request_id"],
            "classifier_id": policy["classifier_id"],
            "provider_request_id": None,
            "usage": None,
            "finish_reason": None,
            "status": status,
        }
        with pytest.raises(ownership.OwnershipProtocolError, match="status"):
            ownership.validate_decision(
                _decision(request, usage_receipt=receipt), request, policy=policy
            )


def test_success_requires_receipt_field():
    request = _request()
    policy = ownership.load_policy()

    successful = _decision(request)
    successful.pop("usage_receipt")
    with pytest.raises(ownership.OwnershipProtocolError, match="include usage_receipt"):
        ownership.validate_decision(successful, request, policy=policy)


def test_subagent_decisions_cannot_override_structured_launch_constraints():
    request = _request()
    response = _decision(
        request,
        ownership="subagent",
        dispatch="spawn_subagent",
    )
    policy = ownership.load_policy()

    assert (
        ownership.validate_decision(response, request, policy=policy)["dispatch"]
        == "spawn_subagent"
    )
    for constraint, value in (
        ("subagents_allowed", False),
        ("externally_assigned", True),
        ("routing_enabled", False),
        ("root_session", False),
        ("available_tools", ["Read"]),
    ):
        constrained_request = _request()
        constrained_request["constraints"][constraint] = value
        constrained_request.pop("input_sha256")
        constrained_request = ownership.validate_request(constrained_request)
        response_for_request = _decision(
            constrained_request,
            ownership="subagent",
            dispatch="spawn_subagent",
        )
        with pytest.raises(ownership.OwnershipProtocolError, match="structured launch constraints"):
            ownership.validate_decision(response_for_request, constrained_request, policy=policy)


def test_completed_same_scope_owner_can_be_reused_but_failed_owner_cannot():
    request = _request()
    request["existing_owners"] = [
        {"agent_id": "agent-real-1", "scope": "inspect function", "state": "completed"}
    ]
    request.pop("input_sha256")
    request = ownership.validate_request(request)
    state = _state("native-session-1")
    state["owners"][0]["state"] = "completed"
    response = _decision(
        request,
        ownership="subagent",
        dispatch="reuse_subagent",
        owner_agent_id="agent-real-1",
        reason_code="continue_existing_owner",
    )

    assert (
        ownership.validate_decision(response, request, state=state)["owner_agent_id"]
        == "agent-real-1"
    )
    state["owners"][0]["state"] = "failed"
    with pytest.raises(
        ownership.OwnershipProtocolError,
        match="same-session running or completed owner",
    ):
        ownership.validate_decision(response, request, state=state)


def test_state_is_session_scoped_atomic_and_monotonic(tmp_path):
    env = {"HOME": str(tmp_path / "private-home")}
    state = _state("native-session-1")

    assert ownership.write_state("native-session-1", state, env=env) == state
    assert ownership.read_state("native-session-1", env={**env}) == state
    assert ownership.write_state("native-session-1", state, env=env) == state
    path, _ = ownership._state_paths("native-session-1", env)
    assert path.parent == tmp_path / "private-home" / ".claude" / "ucode" / "orchestration-state"
    assert path.name != "native-session-1.json"
    assert ownership.read_state("native-session-2", env=env) is None
    other_path, _ = ownership._state_paths("native-session-2", env)
    assert other_path != path
    with pytest.raises(ownership.OwnershipStateError, match="stale"):
        ownership.write_state("native-session-1", _state("native-session-1", generation=0), env=env)
    with pytest.raises(ownership.OwnershipStateError, match="session_id"):
        ownership.write_state("native-session-2", state, env=env)
    invalid_policy = _state("native-session-1", generation=2)
    invalid_policy["current"]["policy_version"] = "different"
    with pytest.raises(ownership.OwnershipStateError, match="policy binding"):
        ownership.write_state("native-session-1", invalid_policy, env=env)


def test_state_uses_private_claude_config_dir_and_rejects_foreign_state(tmp_path):
    env = {
        "HOME": str(tmp_path / "private-home"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "custom-profile"),
    }
    state = _state("native-session-1")
    path, _ = ownership._state_paths("native-session-1", env)
    assert path.parent == tmp_path / "custom-profile" / "ucode" / "orchestration-state"
    ownership.write_state("native-session-1", state, env=env)

    foreign_path, _ = ownership._state_paths("native-session-2", env)
    foreign_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ownership.OwnershipStateError, match="session_id"):
        ownership.read_state("native-session-2", env=env)


def test_state_cli_persists_per_native_session_across_processes(tmp_path):
    env = {
        **os.environ,
        "HOME": str(tmp_path / "private-home"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "private-profile"),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }

    def run_bridge(envelope):
        result = subprocess.run(
            [sys.executable, "-m", "ucode.smart_routing.ownership"],
            input=json.dumps(envelope),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    state = _state("native-resumed-session")
    assert run_bridge(
        {"operation": "state_write", "session_id": state["session_id"], "state": state}
    ) == {"state": state}
    assert run_bridge({"operation": "state_read", "session_id": state["session_id"]}) == {
        "state": state
    }
    assert run_bridge({"operation": "state_read", "session_id": "native-cleared-session"}) == {
        "state": None
    }


def test_instructions_include_each_policy_section_once():
    policy = ownership.load_policy()
    request = _request()
    success = _decision(request)
    spawn = _decision(request, ownership="subagent", dispatch="spawn_subagent")
    reuse = _decision(
        request,
        ownership="subagent",
        dispatch="reuse_subagent",
        owner_agent_id="agent-1",
    )

    for decision in (success, spawn, reuse):
        instructions = ownership.instructions_for_decision(decision, policy, answer_delivery=False)
        assert instructions.count(policy["parent_instructions"]) == 1
        normalized_instructions = " ".join(instructions.split())
        assert _CLASSIFICATION_POLICY_FRAGMENT not in normalized_instructions
        for fragment in _EXECUTION_POLICY_FRAGMENTS:
            assert normalized_instructions.count(fragment) == 1, fragment


def test_unavailable_decision_has_no_instructions():
    policy = ownership.load_policy()
    request = _request()
    unavailable = _decision(
        request,
        decision_status="unavailable",
        ownership=None,
        dispatch=None,
        reason_code="unavailable",
    )

    with pytest.raises(ownership.OwnershipProtocolError, match="decision_status is unsupported"):
        ownership.instructions_for_decision(unavailable, policy, answer_delivery=False)
