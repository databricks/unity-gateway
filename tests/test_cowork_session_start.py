"""Cowork SessionStart hook: token warm-up, upstream probes, and injected guidance."""

import json

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.agents.desktop import cowork_session_start

SERVERS = {
    "github": "https://workspace.example/api/2.0/mcp/github",
    "jira": "https://workspace.example/api/2.0/mcp/jira",
}


@pytest.fixture
def probes(monkeypatch):
    calls = []
    failing = set()

    async def probe(url, token):
        calls.append((url, token))
        if url in failing:
            raise RuntimeError("not ready")

    monkeypatch.setattr(cowork_session_start, "get_databricks_token", lambda *args: "tok")
    monkeypatch.setattr(cowork_session_start, "_probe", probe)
    return calls, failing


def _context(output):
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return output["hookSpecificOutput"]["additionalContext"]


def test_names_connectors_and_points_at_skill_when_all_ready(probes):
    calls, _ = probes

    output = cowork_session_start.session_start_output(
        {"hook_event_name": "SessionStart", "source": "startup"},
        SERVERS,
        "https://workspace.example",
        "chosen",
    )

    context = _context(output)
    assert "`github`, `jira`" in context
    assert "`unity-gateway-connectors` skill" in context
    assert "did not answer" not in context
    assert sorted(calls) == sorted((url, "tok") for url in SERVERS.values())


def test_reports_connectors_that_did_not_answer(probes):
    _, failing = probes
    failing.add(SERVERS["jira"])

    context = _context(
        cowork_session_start.session_start_output(
            {"hook_event_name": "SessionStart"}, SERVERS, "https://workspace.example", None
        )
    )

    assert context.endswith("may still be starting: `jira`.")


def test_slow_probe_is_bounded(probes, monkeypatch):
    import anyio

    async def hang(url, token):
        await anyio.sleep(60)

    monkeypatch.setattr(cowork_session_start, "_probe", hang)
    monkeypatch.setattr(cowork_session_start, "PROBE_TIMEOUT_SECONDS", 0.05)

    context = _context(
        cowork_session_start.session_start_output(
            {"hook_event_name": "SessionStart"}, SERVERS, "https://workspace.example", None
        )
    )

    assert "may still be starting: `github`, `jira`." in context


def test_token_failure_still_injects_guidance_without_probing(probes, monkeypatch):
    calls, _ = probes

    def fail(*args):
        raise RuntimeError("expired")

    monkeypatch.setattr(cowork_session_start, "get_databricks_token", fail)

    context = _context(
        cowork_session_start.session_start_output(
            {"hook_event_name": "SessionStart"}, SERVERS, "https://workspace.example", None
        )
    )

    assert "may still be starting: `github`, `jira`." in context
    assert calls == []


@pytest.mark.parametrize("payload", [{"hook_event_name": "UserPromptSubmit"}, {}, None])
def test_ignores_other_events(probes, payload):
    calls, _ = probes
    assert cowork_session_start.session_start_output(payload, SERVERS, "w", None) == {}
    assert calls == []


def test_cli_writes_only_hook_json_to_stdout(probes):
    result = CliRunner().invoke(
        cli.app,
        [
            "cowork-session-start",
            "--host",
            "https://workspace.example",
            "--profile",
            "chosen",
            "--server",
            f"github={SERVERS['github']}",
        ],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}),
    )

    assert result.exit_code == 0, result.output
    assert "`github`" in _context(json.loads(result.stdout))
