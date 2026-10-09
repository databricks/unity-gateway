"""Offline checks for CUJ bearer minting, leak reporting, terminal waits, and task helpers."""

import gzip
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e_cuj import conftest as cuj_conftest
from tests.e2e_cuj import test_cuj2_mps_mcp as cuj2
from tests.e2e_cuj import test_cuj3_mcp as mcp_registration
from tests.e2e_cuj.base import bearer
from tests.e2e_cuj.helpers import poll as poll_module
from tests.e2e_cuj.helpers.constants import (
    CLAUDE,
    CLAUDE_HAIKU_MODEL,
    CODEX,
    INFERENCE_PATHS,
    CodingAgent,
)
from tests.e2e_cuj.helpers.evidence import assert_models, claude_file_task
from tests.e2e_cuj.helpers.poll import poll
from tests.e2e_cuj.helpers.session import (
    MACHINE_WIDE_LEAK,
    MachineWideLeak,
    UserSession,
    dirty_runner_message,
)
from tests.e2e_cuj.helpers.terminal import Terminal
from tests.e2e_cuj.helpers.workspace import Workspace
from tests.e2e_cuj.test_cuj3_models import _assert_inference_evidence, _catalog_display_names


def _client(headers):
    return SimpleNamespace(config=SimpleNamespace(authenticate=lambda: headers))


def test_bearer_returns_the_token_from_the_authorization_header():
    assert bearer(_client({"Authorization": "Bearer minted-token"})) == "minted-token"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic abc"}],
)
def test_bearer_rejects_a_missing_or_non_bearer_authorization(headers):
    with pytest.raises(RuntimeError, match="did not return a bearer token"):
        bearer(_client(headers))


def test_refresh_bearer_keeps_a_journey_selected_workspace_bearer(tmp_path):
    session = UserSession(tmp_path, Path("/installed/ug"), tmp_path / "artifacts", "first")
    session.refresh_bearer("second")
    assert session.env["DATABRICKS_BEARER"] == "second"
    session.install_bearer("phase-b")
    session.refresh_bearer("third")
    assert session.env["DATABRICKS_BEARER"] == "phase-b"


def test_redact_scrubs_bearers_replaced_by_a_refresh_or_phase_switch(tmp_path):
    session = UserSession(tmp_path, Path("/installed/ug"), tmp_path / "artifacts", "first-token")
    session.refresh_bearer("second-token")
    session.install_bearer("phase-b-token")
    redacted = session.redact("first-token second-token phase-b-token")
    assert redacted == "<redacted> <redacted> <redacted>"


def _leak_fixture(tmp_path, monkeypatch, leaked_names):
    managed = tmp_path / "etc"
    managed.mkdir()
    paths = tuple(managed / name for name in ("claude.json", "codex.toml"))
    for path in paths:
        if path.name in leaked_names:
            path.write_text("{}")
    monkeypatch.setattr(cuj_conftest, "MANAGED_PATHS", paths)
    request = SimpleNamespace(
        config=SimpleNamespace(stash=pytest.Stash()),
        node=SimpleNamespace(nodeid="tests/e2e_cuj/test_x.py::TestLeaky"),
    )
    return request, paths


def test_teardown_leak_names_the_culprit(tmp_path, monkeypatch):
    request, paths = _leak_fixture(tmp_path, monkeypatch, {"claude.json"})

    cuj_conftest._record_machine_wide_leak(request)

    leak = request.config.stash[MACHINE_WIDE_LEAK]
    assert leak == MachineWideLeak("tests/e2e_cuj/test_x.py::TestLeaky", (str(paths[0]),))
    assert paths[0].exists(), "Leak handling must not touch machine-wide files"
    message = dirty_runner_message(leak)
    assert "TestLeaky" in message
    assert repr(str(paths[0])) in message


def test_clean_teardown_records_no_leak(tmp_path, monkeypatch):
    request, _ = _leak_fixture(tmp_path, monkeypatch, set())
    cuj_conftest._record_machine_wide_leak(request)
    leak = request.config.stash.get(MACHINE_WIDE_LEAK, None)
    assert leak is None
    assert dirty_runner_message(leak) == (
        "Existing machine-wide agent settings; use a clean disposable runner. Nothing was changed."
    )


requires_pty = pytest.mark.skipif(sys.platform == "win32", reason="Terminal needs a POSIX PTY")


def _fake_ug(tmp_path, body):
    """A real executable standing in for `ug`, so the real PTY driver runs offline."""
    script = tmp_path / "ug"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return UserSession(tmp_path, script, tmp_path / "artifacts", "token")


@pytest.mark.parametrize(
    ("args", "agent"),
    [([], None), ([], "gemini"), (["status"], None), ([CLAUDE], CODEX)],
)
def test_terminal_rejects_a_missing_unsupported_or_mismatched_agent(tmp_path, args, agent):
    session = UserSession(tmp_path, Path("/installed/ug"), tmp_path / "artifacts", "token")
    with pytest.raises(ValueError, match="requires a Claude or Codex command or bare ug agent"):
        Terminal(session, "rejected", args, agent=agent)


@requires_pty
def test_wait_until_rejects_a_permission_prompt(tmp_path):
    session = _fake_ug(tmp_path, "echo 'Do you want to proceed?'\nsleep 30\n")
    with Terminal(session, "rejects", [], agent=CLAUDE) as tui:
        with pytest.raises(AssertionError, match="Unexpected permission request"):
            tui.wait_until(lambda: False, "never", timeout=10)


@requires_pty
def test_wait_until_completes_when_done(tmp_path):
    done = tmp_path / "done"
    session = _fake_ug(tmp_path, f"echo working\ntouch '{done}'\nsleep 30\n")
    with Terminal(session, "completes", [], agent=CODEX) as tui:
        tui.wait_until(done.exists, "the done marker", timeout=10)
        assert "working" in tui.visible


@requires_pty
def test_wait_until_lets_on_screen_answer_an_expected_dialog(tmp_path):
    done = tmp_path / "done"
    session = _fake_ug(
        tmp_path, f"echo 'Allow this call?'\nread answer\necho allowed\ntouch '{done}'\nsleep 30\n"
    )
    answered = []

    def approve(screen):
        if "Allow this call?" in screen and not answered:
            tui.send("\r", "allow the expected call")
            answered.append(screen)
            return True
        return False

    with Terminal(session, "approves", [], agent=CODEX) as tui:
        tui.wait_until(done.exists, "the approved call", timeout=10, on_screen=approve)
        assert len(answered) == 1
        assert "allowed" in tui.visible


def test_mcp_list_poll_retries_a_failed_probe_until_rows_match(monkeypatch):
    monkeypatch.setattr(poll_module.time, "sleep", lambda seconds: None)
    healthy = "\n".join(
        rows[0] for rows in mcp_registration._expected_fixture_server_rows().values()
    )
    failed = healthy.replace(f"{CLAUDE}, {CODEX} connected", f"{CLAUDE}:failed {CODEX}:enabled", 1)
    outputs = [failed, failed, healthy, "unused"]
    session = SimpleNamespace(
        run=lambda *args, timeout: SimpleNamespace(stdout=outputs.pop(0), stderr="")
    )
    output = mcp_registration._poll_mcp_list(session)
    assert output == f"{healthy}\n"
    assert outputs == ["unused"]


def test_claude_file_task_names_the_absolute_path_without_the_answer(tmp_path):
    session = UserSession(tmp_path, Path("/installed/ug"), tmp_path / "artifacts", "token")
    task = claude_file_task(session)
    path = session.cwd / task.filename
    assert path.read_text() == task.value + "\n"
    assert str(path) in task.prompt
    assert task.value not in task.prompt


def test_poll_returns_the_first_accepted_value():
    values = iter([0, 0, 2, 5])
    assert poll(lambda: next(values), timeout=10, interval=0) == 2


def test_poll_returns_the_last_value_at_the_timeout():
    calls = []
    assert poll(lambda: calls.append(1) or (0, 1), timeout=0, interval=0, done=all) == (
        0,
        1,
    )
    assert calls == [1]


def test_agent_configs_rejects_a_duplicate_agent():
    entry = {"agent": CodingAgent.CODEX, "config": {}}
    with pytest.raises(AssertionError, match="Duplicate enabled agents"):
        Workspace.agent_configs({"enabled_agents": [entry, entry]})
    assert Workspace.agent_configs({"enabled_agents": [entry]}) == {CodingAgent.CODEX: {}}


def _catalog_workspace(pages):
    calls = []

    def do(method, path, headers, query):
        calls.append((path, dict(headers), dict(query)))
        return pages.pop(0)

    workspace = SimpleNamespace(client=SimpleNamespace(api_client=SimpleNamespace(do=do)))
    return workspace, calls


def _anthropic_page(models, last_id=None):
    return {
        "data": [{"id": model, "display_name": model.upper()} for model in models],
        "has_more": last_id is not None,
        "last_id": last_id,
    }


def test_catalog_display_names_follows_the_anthropic_cursor():
    workspace, calls = _catalog_workspace([_anthropic_page(["a"], "a"), _anthropic_page(["b"])])
    assert _catalog_display_names(workspace, CLAUDE, "ug_e2e.models") == {"a": "A", "b": "B"}
    assert [query for _, _, query in calls] == [
        {"limit": "1000"},
        {"limit": "1000", "after_id": "a"},
    ]
    assert calls[0][1]["Anthropic-Version"] == "2023-06-01"


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ([_anthropic_page(["a"], "a"), _anthropic_page(["a"], "b")], "Repeated catalog model"),
        ([_anthropic_page(["a"], "x"), _anthropic_page(["b"], "x")], "pagination cursor"),
    ],
)
def test_catalog_display_names_rejects_repeated_pages(pages, message):
    workspace, _ = _catalog_workspace(pages)
    with pytest.raises(AssertionError, match=message):
        _catalog_display_names(workspace, CLAUDE, "ug_e2e.models")


@pytest.fixture
def thinking_display_exchange():
    model = "ug_e2e.models.claude_sonnet"
    task = SimpleNamespace(prompt="Read the task file")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": task.prompt}],
        "thinking": {"type": "adaptive", "display": "updates"},
        "output_config": {"effort": "high"},
    }
    requests = [
        SimpleNamespace(method="POST", path=INFERENCE_PATHS[CLAUDE], payload=payload),
        SimpleNamespace(
            method="POST",
            path=INFERENCE_PATHS[CLAUDE],
            payload={**payload, "thinking": {"type": "adaptive"}},
        ),
    ]
    error = json.dumps(
        {
            "error_code": "BAD_REQUEST",
            "message": json.dumps(
                {"message": "thinking.adaptive.display: Input should be 'summarized', 'omitted'"}
            ),
        }
    ).encode()
    responses = [
        SimpleNamespace(status_code=400, headers={}, body=error),
        SimpleNamespace(status_code=200, headers={}, body=b"successful stream"),
    ]
    recorder = SimpleNamespace(
        requests_after=lambda checkpoint: requests,
        response_for=lambda request, timeout: responses[requests.index(request)],
    )
    return recorder, requests, responses, task, model


@pytest.mark.parametrize("compressed", [False, True])
def test_catalog_inference_accepts_verified_thinking_display_recovery(
    thinking_display_exchange, compressed
):
    recorder, _, responses, task, model = thinking_display_exchange
    if compressed:
        responses[0].body = gzip.compress(responses[0].body)
        responses[0].headers = {"content-encoding": "gzip"}
    _assert_inference_evidence(recorder, 0, CLAUDE, task, model)


@pytest.mark.parametrize(
    "failure",
    [
        "unrelated_400",
        "malformed_error",
        "server_error",
        "missing_retry",
        "failed_retry",
        "empty_retry",
        "changed_model",
        "changed_effort",
        "changed_prompt",
        "display_retained",
        "wrong_display",
        "codex",
    ],
)
def test_catalog_inference_rejects_unverified_recovery(thinking_display_exchange, failure):
    recorder, requests, responses, task, model = thinking_display_exchange
    agent = CLAUDE
    if failure == "unrelated_400":
        responses[0].body = responses[0].body.replace(b"thinking.adaptive.display", b"other.field")
    elif failure == "malformed_error":
        responses[0].body = b"not JSON"
    elif failure == "server_error":
        responses[0].status_code = 500
    elif failure == "missing_retry":
        requests.pop()
    elif failure == "failed_retry":
        responses[1].status_code = 400
    elif failure == "empty_retry":
        responses[1].body = b""
    elif failure == "changed_model":
        requests[1].payload["model"] = "ug_e2e.models.claude_haiku"
    elif failure == "changed_effort":
        requests[1].payload["output_config"] = {}
    elif failure == "changed_prompt":
        requests[1].payload["messages"] = [{"role": "user", "content": "A different task"}]
    elif failure == "display_retained":
        requests[1].payload["thinking"] = {"type": "adaptive", "display": "updates"}
    elif failure == "wrong_display":
        requests[0].payload["thinking"] = {"type": "adaptive", "display": "summarized"}
    elif failure == "codex":
        agent = CODEX
        for request in requests:
            request.path = INFERENCE_PATHS[CODEX]
            request.payload["input"] = task.prompt
    with pytest.raises(AssertionError):
        _assert_inference_evidence(recorder, 0, agent, task, model)


def test_assert_models_maps_native_aliases():
    alias = "anthropic.claude-haiku-4-5-20251001-v1:0"
    assert_models([alias, CLAUDE_HAIKU_MODEL], CLAUDE_HAIKU_MODEL)
    with pytest.raises(AssertionError):
        assert_models([CLAUDE_HAIKU_MODEL, "system.ai.claude-sonnet-4-6"], CLAUDE_HAIKU_MODEL)


@pytest.mark.parametrize(
    "shape",
    ["bare", "native_keys", "raw_result_string", "raw_result_object"],
)
def test_mcp_receipts_accept_native_keys_and_raw_tool_results(shape):
    task = mcp_registration.McpFixtureTask()
    native = {tool: key for key, tool in task.aliases.items()}
    observed = {
        "bare": dict(task.expected),
        "native_keys": {native[tool]: value for tool, value in task.expected.items()},
        "raw_result_string": {
            tool: json.dumps({"result": value}) for tool, value in task.expected.items()
        },
        "raw_result_object": {tool: {"result": value} for tool, value in task.expected.items()},
    }[shape]

    assert task._receipts(observed) == task.expected


def test_mcp_receipts_reject_wrong_extra_or_duplicate_receipts():
    task = mcp_registration.McpFixtureTask()
    describe, read = task.expected
    native = {tool: key for key, tool in task.aliases.items()}

    assert task._receipts({**task.expected, read: "0" * 64}) != task.expected
    assert task._receipts({**task.expected, "extra": "x"}) != task.expected
    assert (
        task._receipts(
            {describe: {"result": task.expected[describe], "x": 1}, read: task.expected[read]}
        )
        != task.expected
    )
    assert task._receipts({**task.expected, native[describe]: task.expected[describe]}) is None
    assert task._receipts(["not", "a", "mapping"]) is None


def test_mcp_completion_accepts_a_fenced_reply(monkeypatch):
    task = mcp_registration.McpFixtureTask()
    reply = "```json\n" + json.dumps(task.expected) + "\n```"
    monkeypatch.setattr(mcp_registration, "agent_sessions", lambda session, agent: {"s": []})
    monkeypatch.setattr(mcp_registration, "assistant_answers", lambda agent, records: [reply])

    assert task.completed(None, "claude")


def test_cuj2_mcp_listing_retries_a_banner_only_claude_listing(tmp_path, monkeypatch):
    suffix = ".EXE" if sys.platform == "win32" else ""
    for agent in (CLAUDE, CODEX):
        (tmp_path / f"{agent}{suffix}").write_text("#!/bin/sh\n")
        (tmp_path / f"{agent}{suffix}").chmod(0o755)
    banner = "Checking MCP server health…\n\n\n"
    rows = f"{cuj2.registered_name(cuj2.SANDBOX_MCP_SERVICE_NAME)}: ug mcp-proxy - ✓ Connected\n"
    outputs = {CLAUDE: [banner, rows], CODEX: [rows]}
    calls = []

    def run(*args, binary, timeout):
        agent = Path(binary).stem
        calls.append(agent)
        return SimpleNamespace(stdout=outputs[agent].pop(0), stderr="")

    monkeypatch.setattr(poll_module.time, "sleep", lambda seconds: None)
    cuj2._assert_generated_mcp_listings(SimpleNamespace(env={"PATH": str(tmp_path)}, run=run))

    assert calls == [CLAUDE, CLAUDE, CODEX]
