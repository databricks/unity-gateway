"""Codex access-token login and a simple inference request through the ChatGPT relay."""

import json

import pytest
from utils.constants import CODEX_RELAYED_MODEL
from utils.evidence import fresh_id

pytestmark = [pytest.mark.live, pytest.mark.codex, pytest.mark.codex_relay]


def test_ug_codex_relayed_completes_inference_through_the_relay(
    live_session, workspace, codex_relayed_provider, codex_access_token
):
    """Scenario: log in with an access token and ask Codex for a short reply through a relay MPS.

    Expected: native login succeeds, Codex completes a turn with the requested answer, and
    the loopback proxy forwards /v1/responses with HTTP 200. File tools, desktop configuration,
    and browser sign-in are outside this check.
    """
    session = live_session
    (session.home / ".codex").mkdir(exist_ok=True)
    session.env["CODEX_ACCESS_TOKEN"] = codex_access_token
    login = session.run(
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
        "--with-access-token",
        binary=session.which("codex"),
        input_text=codex_access_token,
        timeout=60,
    )
    assert "Successfully logged in" in login.stdout + login.stderr
    session.record("model.json", {"model": CODEX_RELAYED_MODEL})
    answer = fresh_id()
    result = session.run(
        "codex",
        "--workspace",
        workspace,
        "--provider",
        codex_relayed_provider,
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        "-m",
        CODEX_RELAYED_MODEL,
        f"Reply with exactly {answer} and nothing else. Do not use tools.",
        timeout=180,
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(event.get("type") == "turn.completed" for event in events), result.stdout
    answers = [
        event["item"]["text"].strip()
        for event in events
        if event.get("type") == "item.completed"
        and event.get("item", {}).get("type") == "agent_message"
    ]
    assert answers == [answer], result.stdout
    diagnostics = [
        json.loads(line.removeprefix("[ucode-relay] "))
        for line in result.stderr.splitlines()
        if line.startswith("[ucode-relay] ")
    ]
    requests = {
        event["request_id"]
        for event in diagnostics
        if event.get("event") == "request_start"
        and event.get("path") == "/v1/responses"
        and event.get("route") == "forward"
    }
    assert any(
        event.get("event") == "response_complete"
        and event.get("request_id") in requests
        and event.get("status") == 200
        for event in diagnostics
    ), result.stderr
