"""Cowork connector hooks: token warm-up, upstream probes, idle detection, and guidance."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.agents.desktop import cowork_hooks

SERVERS = {
    "github": "https://workspace.example/api/2.0/mcp/github",
    "jira": "https://workspace.example/api/2.0/mcp/jira",
}
NOW = datetime(2026, 10, 28, 14, 2, tzinfo=UTC)


@pytest.fixture
def probes(monkeypatch):
    calls = []
    failing = set()

    async def probe(url, token, timeout):
        calls.append((url, token))
        if url in failing:
            raise RuntimeError("not ready")

    monkeypatch.setattr(cowork_hooks, "get_databricks_token", lambda *args: "tok")
    monkeypatch.setattr(cowork_hooks, "_probe", probe)
    return calls, failing


def _context(output, event):
    assert output["hookSpecificOutput"]["hookEventName"] == event
    return output["hookSpecificOutput"]["additionalContext"]


def _session_start(**kwargs):
    return cowork_hooks.hook_output(
        {"hook_event_name": "SessionStart", "source": "startup"},
        SERVERS,
        "https://workspace.example",
        "chosen",
        **kwargs,
    )


def _transcript(tmp_path, *entries):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return str(path)


def _prompt(transcript_path):
    return cowork_hooks.hook_output(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "search online sources",
            "transcript_path": transcript_path,
        },
        SERVERS,
        "https://workspace.example",
        "chosen",
        now=NOW,
    )


def _reply_at(minutes_ago):
    stamp = (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"type": "assistant", "timestamp": stamp}


def test_session_start_names_connectors_and_points_at_skill(probes):
    calls, _ = probes

    context = _context(_session_start(), "SessionStart")

    assert "`github`, `jira`" in context
    assert "`unity-gateway-connectors` skill" in context
    assert "did not answer" not in context
    assert sorted(calls) == sorted((url, "tok") for url in SERVERS.values())


def test_session_start_reports_connectors_that_did_not_answer(probes):
    _, failing = probes
    failing.add(SERVERS["jira"])

    assert _context(_session_start(), "SessionStart").endswith("may still be starting: `jira`.")


def test_slow_probe_is_bounded(probes, monkeypatch):
    import anyio

    async def hang(url, token, timeout):
        await anyio.sleep(60)

    monkeypatch.setattr(cowork_hooks, "_probe", hang)
    monkeypatch.setattr(cowork_hooks, "SESSION_START_PROBE_TIMEOUT_SECONDS", 0.05)

    assert "may still be starting: `github`, `jira`." in _context(_session_start(), "SessionStart")


def test_token_failure_still_injects_guidance_without_probing(probes, monkeypatch):
    calls, _ = probes

    def fail(*args):
        raise RuntimeError("expired")

    monkeypatch.setattr(cowork_hooks, "get_databricks_token", fail)

    assert "may still be starting: `github`, `jira`." in _context(_session_start(), "SessionStart")
    assert calls == []


@pytest.mark.parametrize("payload", [{"hook_event_name": "Stop"}, {}, None])
def test_ignores_other_events(probes, payload):
    calls, _ = probes
    assert cowork_hooks.hook_output(payload, SERVERS, "w", None) == {}
    assert calls == []


def test_prompt_after_short_gap_does_nothing(probes, tmp_path):
    calls, _ = probes
    # The current prompt may already be recorded; only Claude's last reply counts.
    path = _transcript(
        tmp_path, _reply_at(1), {"type": "user", "timestamp": NOW.isoformat()}, {"type": "mode"}
    )

    assert _prompt(path) == {}
    assert calls == []


def test_prompt_after_idle_gap_probes_and_flags_stale_notices(probes, tmp_path):
    calls, failing = probes
    failing.add(SERVERS["jira"])
    path = _transcript(tmp_path, _reply_at(30), _reply_at(6), "not json")

    context = _context(_prompt(path), "UserPromptSubmit")

    assert "stale" in context
    assert "`unity-gateway-connectors` skill" in context
    assert "At 14:02 UTC these connectors answered: `github`." in context
    assert context.endswith("These did not answer: `jira`.")
    assert len(calls) == 2


def test_prompt_after_idle_gap_without_token_keeps_stale_notice_guidance(
    probes, tmp_path, monkeypatch
):
    def fail(*args):
        raise RuntimeError("expired")

    monkeypatch.setattr(cowork_hooks, "get_databricks_token", fail)

    context = _context(_prompt(_transcript(tmp_path, _reply_at(6))), "UserPromptSubmit")

    assert "stale" in context
    assert "answered" not in context


@pytest.mark.parametrize("transcript", ["missing", "no-reply", None])
def test_prompt_without_previous_reply_does_nothing(probes, tmp_path, transcript):
    calls, _ = probes
    path = {
        "missing": str(tmp_path / "absent.jsonl"),
        "no-reply": _transcript(tmp_path, {"type": "user", "timestamp": "2026-10-28T13:00:00Z"}),
        None: None,
    }[transcript]

    assert _prompt(path) == {}
    assert calls == []


def test_cli_writes_only_hook_json_to_stdout(probes):
    result = CliRunner().invoke(
        cli.app,
        [
            "cowork-hook",
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
    assert "`github`" in _context(json.loads(result.stdout), "SessionStart")
