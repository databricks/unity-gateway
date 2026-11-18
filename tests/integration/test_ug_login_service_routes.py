"""CUJ: ug's login service serves the GitHub Copilot app's model turns on its shared port."""

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest
from utils.constants import CLAUDE_TEST_MODEL, COPILOT_GPT_TEST_MODEL
from utils.harness import process_group_options, stop_process

pytestmark = [pytest.mark.live, pytest.mark.copilot]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post(url: str, body: dict, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _wait_for_port(proc: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert proc.poll() is None, proc.communicate(timeout=5)
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    pytest.fail(f"ug otel-proxy did not listen on {port}")


def test_ug_login_service_serves_copilot_app_turns(live_session, workspace):
    """Scenario: configure Copilot, run ug's login service the way its desktop unit does, and
    send the streamed Claude and GPT turns the GitHub Copilot app sends to its two providers.

    Expected: both turns stream a completed answer from the workspace gateway although the app
    holds no Databricks credential; any other path, and any browser request, is refused.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    port = _free_port()
    proc = subprocess.Popen(
        [str(session.binary), "otel-proxy", "--host", workspace, "--port", str(port)],
        cwd=session.cwd,
        env=session.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        **process_group_options(),
    )
    base = f"http://127.0.0.1:{port}"
    prompt = "Reply with the single word ready."
    try:
        _wait_for_port(proc, port)
        claude_status, claude = _post(
            f"{base}/copilot/anthropic/v1/messages",
            {
                "model": CLAUDE_TEST_MODEL,
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": prompt}],
            },
            {"x-api-key": "placeholder-not-a-secret", "anthropic-version": "2023-06-01"},
        )
        gpt_status, gpt = _post(
            f"{base}/copilot/responses/v1/responses",
            {"model": COPILOT_GPT_TEST_MODEL, "input": prompt, "stream": True},
        )
        other_status, _ = _post(f"{base}/api/2.0/clusters/list", {})
        browser_status, _ = _post(
            f"{base}/copilot/anthropic/v1/messages",
            {"model": CLAUDE_TEST_MODEL, "max_tokens": 8, "messages": []},
            {"Origin": "https://example.com"},
        )
    finally:
        stop_process(proc)
        _, stderr = proc.communicate(timeout=10)
        session.record("login-service.json", {"stderr": stderr})
    session.record(
        "turns.json",
        {
            "claude": [claude_status, claude.decode(errors="replace")[-2000:]],
            "gpt": [gpt_status, gpt.decode(errors="replace")[-2000:]],
        },
    )
    assert claude_status == 200, claude
    assert b"event: message_stop" in claude
    assert gpt_status == 200, gpt
    assert b"response.completed" in gpt
    assert other_status == 404
    assert browser_status == 403
