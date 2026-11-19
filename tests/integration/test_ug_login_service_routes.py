"""CUJ: ug's login service serves the GitHub Copilot app's model turns on its shared port."""

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest
from utils.constants import CLAUDE_TEST_MODEL, COPILOT_GLM_TEST_MODEL, COPILOT_GPT_TEST_MODEL
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
        assert proc.poll() is None, "ug otel-proxy exited; see login-service.log"
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    pytest.fail(f"ug otel-proxy did not listen on {port}")


def test_ug_login_service_serves_copilot_app_turns(live_session, workspace):
    """Scenario: configure Copilot, run ug's login service the way its desktop unit does, and
    send the streamed Claude, GPT and open-model turns the GitHub Copilot app sends to its three
    providers. The open-model turn carries the `reasoning_effort` Copilot sends.

    Expected: all three turns stream a completed answer from the workspace gateway although the app
    holds no Databricks credential; any other path is refused.
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
    # The app's routes are behind this flag until the feature ships.
    session.env["UG_EXPERIMENTAL_COPILOT_APP"] = "1"
    port = _free_port()
    # A file, not a pipe: nothing reads the service's log until it stops, and a full pipe
    # (4 KB on Windows) would block it mid-request.
    log_path = session.artifacts / "login-service.log"
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(session.binary), "otel-proxy", "--host", workspace, "--port", str(port)],
        cwd=session.cwd,
        env=session.env,
        stdout=log,
        stderr=subprocess.STDOUT,
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
        glm_status, glm = _post(
            f"{base}/copilot/completions/v1/chat/completions",
            {
                "model": COPILOT_GLM_TEST_MODEL,
                "stream": True,
                "reasoning_effort": "none",
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        other_status, _ = _post(f"{base}/api/2.0/clusters/list", {})
    finally:
        stop_process(proc)
        proc.wait(timeout=10)
        log.close()
        session.record(
            "login-service.json", {"output": log_path.read_text(encoding="utf-8", errors="replace")}
        )
    session.record(
        "turns.json",
        {
            "claude": [claude_status, claude.decode(errors="replace")[-2000:]],
            "gpt": [gpt_status, gpt.decode(errors="replace")[-2000:]],
            "glm": [glm_status, glm.decode(errors="replace")[-2000:]],
        },
    )
    assert claude_status == 200, claude
    assert b"event: message_stop" in claude
    assert gpt_status == 200, gpt
    assert b"response.completed" in gpt
    assert glm_status == 200, glm
    assert b'"choices"' in glm
    assert other_status == 404
