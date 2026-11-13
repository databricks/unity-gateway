"""Control HTTP transport to test routing failures without credentials or live service calls."""

import io
import json
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from uuid import UUID

import pytest

from ucode.smart_routing import routing

BODY = b'{"route_selection":[{"route_option":{"model":"model-a"}}],"rationale":"private-rationale"}'


class Response(io.BytesIO):
    status = 200

    def __init__(self, body=BODY, *, read_error=None, request_id="server-1"):
        super().__init__(body)
        self.read_error = read_error
        self.headers = Message()
        self.headers["X-Request-Id"] = request_id
        self.headers["X-Databricks-Request-Id"] = "databricks-1"
        self.headers["Authorization"] = "private-header"

    def read(self, *args):
        if self.read_error:
            raise self.read_error
        return super().read(*args)


def select():
    return routing.select_route(
        "https://private-workspace.example",
        "private-token",
        "private-task",
        [("model-a", "codex")],
        router_name="stable",
        timeout=7,
    )


def records(capsys):
    output = capsys.readouterr()
    assert output.out == ""
    rows = [
        json.loads(line.removeprefix("[ug-routing] "))
        for line in output.err.splitlines()
        if line.startswith("[ug-routing] ")
    ]
    assert rows and all(row["v"] == 1 and row["at"] > 0 for row in rows)
    assert not any(
        secret in json.dumps(rows)
        for secret in (
            "private-",
            "model-a",
            "https://",
            "Authorization",
            "server_ms",
            "tls_ms",
        )
    )
    return rows


@pytest.fixture
def transport(monkeypatch):
    def install(result):
        def urlopen(request, timeout):
            assert timeout == 7
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(routing.urllib.request, "urlopen", urlopen)

    return install


@pytest.mark.parametrize(
    "header", ["X-Client-Request-Id", "x-client-request-id", "X-CLIENT-REQUEST-ID"]
)
def test_success_starts_before_network_and_correlates_each_attempt(monkeypatch, capsys, header):
    monkeypatch.setenv("UG_HEADERS", header + ": inherited-id")
    starts, now = [], [0.0]
    monkeypatch.setattr(routing.time, "monotonic", lambda: now[0])

    def prepare(workspace):
        now[0] += 500  # Simulate workspace discovery before the observed request.
        return {}

    monkeypatch.setattr(routing, "workspace_headers", prepare)

    def urlopen(request, timeout):
        (start,) = records(capsys)
        assert start["event"] == "request_start" and start["elapsed_ms"] == 0
        assert start["outcome"] == "started" and start["phase"] == "open_response"
        assert UUID(start["client_request_id"]).version == 4
        assert request.get_header("X-client-request-id") == start["client_request_id"]
        assert timeout == start["timeout_s"] == 7
        starts.append(start)
        now[0] += 1
        return Response()

    monkeypatch.setattr(routing.urllib.request, "urlopen", urlopen)
    for _ in range(2):
        decision, reason = select()
        assert decision.model == "model-a" and reason is None
        (finish,) = records(capsys)
        assert finish["event"] == "request_finish" and finish["outcome"] == "selected"
        assert finish["client_request_id"] == starts[-1]["client_request_id"]
        assert finish["http_status"] == 200 and finish["server_request_id"] == "server-1"
        assert finish["route_option_count"] == 1 and finish["router_name"] == "stable"
        assert finish["phase"] == "validate_selection" and finish["elapsed_ms"] == 1000
        assert finish["open_response_ms"] == 1000
        assert all(
            finish[phase + "_ms"] >= 0
            for phase in ("open_response", "read_body", "decode_response", "validate_selection")
        )
    assert starts[0]["client_request_id"] != starts[1]["client_request_id"]


@pytest.mark.parametrize(
    "case,phase,outcome,status,error_type",
    [
        ("open", "open_response", "timeout", None, "TimeoutError"),
        ("wrapped", "open_response", "timeout", None, "TimeoutError"),
        ("network", "open_response", "transport_error", None, "URLError"),
        ("read", "read_body", "timeout", 200, "TimeoutError"),
        ("http", "read_body", "http_error", 503, "HTTPError"),
        ("http_read", "read_body", "http_error", 503, "HTTPError"),
        ("json", "decode_response", "invalid_json", 200, "JSONDecodeError"),
        ("missing", "validate_selection", "no_selection", 200, None),
        ("unoffered", "validate_selection", "unoffered_model", 200, None),
        ("unexpected", "open_response", "unexpected_error", None, "RuntimeError"),
    ],
)
def test_failure_is_safe_and_preserves_return(
    transport, capsys, case, phase, outcome, status, error_type
):
    source = {
        "open": TimeoutError("private-timeout"),
        "wrapped": URLError(TimeoutError("private-timeout")),
        "network": URLError("private-network"),
        "read": Response(read_error=TimeoutError("private-read")),
        "http": HTTPError(
            "private-url", 503, "private-error", Response().headers, io.BytesIO(b"private-body")
        ),
        "http_read": HTTPError(
            "private-url",
            503,
            "private-error",
            Response().headers,
            Response(read_error=TimeoutError()),
        ),
        "json": Response(b"{"),
        "missing": Response(b"{}"),
        "unoffered": Response(BODY.replace(b"model-a", b"private-model")),
        "unexpected": RuntimeError("private-unexpected"),
    }[case]
    transport(source)
    if case == "unexpected":
        with pytest.raises(RuntimeError, match="private-unexpected"):
            select()
    else:
        decision, error = select()
        assert decision is None
        if phase == "open_response":
            assert error == f"router request failed: {source}"
        elif case == "read":
            assert error == "router request failed: private-read"
        elif case.startswith("http"):
            assert error == "router returned HTTP 503" + (
                ": private-body" if case == "http" else ""
            )
        elif case == "missing":
            assert error == "router returned no model selection"
        elif case == "unoffered":
            assert error == "router selected a model it was not offered: 'private-model'"
        else:
            assert error.startswith("router request failed:")
    start, finish = records(capsys)
    assert start["client_request_id"] == finish["client_request_id"]
    assert (finish["phase"], finish["outcome"], finish["http_status"]) == (phase, outcome, status)
    assert finish["event"] == "request_finish" and finish["error_type"] == error_type
    assert finish["server_request_id"] == ("server-1" if status else None)
    if phase == "open_response":
        assert "read_body_ms" not in finish and "decode_response_ms" not in finish


@pytest.mark.parametrize("request_id", ["private-header!", "x" * 129, ""])
def test_unsafe_response_id_uses_validated_fallback(transport, capsys, request_id):
    transport(Response(request_id=request_id))
    assert select()[1] is None
    assert records(capsys)[-1]["server_request_id"] == "databricks-1"


def test_response_without_status_or_headers_remains_supported(transport, capsys):
    response = io.BytesIO(BODY)
    transport(response)
    assert select()[1] is None
    finish = records(capsys)[-1]
    assert finish["http_status"] is None and finish["server_request_id"] is None


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_broken_stderr_preserves_results_and_exceptions(transport, monkeypatch, failure):
    def broken(*args):
        raise OSError("private-stderr")

    stderr = SimpleNamespace(
        write=lambda value: len(value), flush=lambda: None, isatty=lambda: False
    )
    setattr(stderr, failure, broken)
    monkeypatch.setattr(routing.sys, "stderr", stderr)
    transport(Response())
    assert select()[1] is None
    transport(TimeoutError("private-timeout"))
    assert select() == (None, "router request failed: private-timeout")
    transport(RuntimeError("private-unexpected"))
    with pytest.raises(RuntimeError, match="private-unexpected"):
        select()


@pytest.mark.parametrize("attached", [False, True])
def test_missing_or_interactive_stderr_preserves_routing_without_json(
    transport, monkeypatch, attached
):
    stderr = io.StringIO() if attached else None
    if attached:
        stderr.isatty = lambda: True
    monkeypatch.setattr(routing.sys, "stderr", stderr)
    transport(Response())
    assert select()[1] is None
    transport(TimeoutError("private-timeout"))
    assert select() == (None, "router request failed: private-timeout")
    transport(RuntimeError("private-unexpected"))
    with pytest.raises(RuntimeError, match="private-unexpected"):
        select()
    if stderr is not None:
        assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    "mode", ["resolved", "no_decision", "unexpected_error", "mapper_error", "nested"]
)
def test_hook_scope_joins_requests_and_resets_after_every_exit(transport, capsys, mode):
    transport(Response())

    def decision(task):
        if mode == "nested":
            assert (
                routing.resolve_spawn_route(
                    {"tool_name": "Agent", "tool_input": {}, "tool_use_id": "private-invalid!"},
                    is_spawn_agent=lambda name: True,
                    decision_fn=lambda task: (None, None),
                    default_task_label="private-task",
                    model_id_mapper=mapper,
                )
                is None
            )
        if mode == "unexpected_error":
            raise RuntimeError("private-decision")
        return (None, "private-reason") if mode == "no_decision" else select()

    def mapper(model):
        if mode == "mapper_error":
            raise RuntimeError("private-mapper")
        return "mapped"

    def resolve(payload):
        return routing.resolve_spawn_route(
            payload,
            is_spawn_agent=lambda name: name == "Agent",
            decision_fn=decision,
            default_task_label="private-task",
            model_id_mapper=mapper,
        )

    payload = {"tool_name": "Agent", "tool_input": {}, "tool_use_id": "call-1"}
    if mode in {"unexpected_error", "mapper_error"}:
        with pytest.raises(RuntimeError, match="private-"):
            resolve(payload)
    else:
        result = resolve(payload)
        assert result.routed_model == "mapped" if mode in {"resolved", "nested"} else result is None
    rows = records(capsys)
    assert rows[0]["event"] == "hook_start" and rows[-1]["event"] == "hook_finish"
    assert rows[-1]["outcome"] == (
        "unexpected_error" if mode == "mapper_error" else "resolved" if mode == "nested" else mode
    )
    assert UUID(rows[0]["hook_id"]).version == 4
    if mode == "nested":
        assert rows[1]["hook_id"] == rows[2]["hook_id"] != rows[0]["hook_id"]
        assert rows[1]["tool_use_id"] is None and rows[2]["outcome"] == "no_decision"
        rows = rows[:1] + rows[3:]
    assert all(
        row["hook_id"] == rows[0]["hook_id"] and row["tool_use_id"] == "call-1" for row in rows
    )
    transport(Response())
    assert select()[1] is None
    assert all(row["hook_id"] is None and row["tool_use_id"] is None for row in records(capsys))
    assert resolve({}) is None and resolve({"tool_name": "Agent"}) is None
    assert capsys.readouterr().err == ""
