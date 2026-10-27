"""CUJ for the loopback bridge that Copilot Chat in VS Code reaches the gateway through."""

import http.client
import json
import socket
import subprocess
import time

import pytest
from utils.constants import CLAUDE_TEST_MODEL
from utils.evidence import assert_no_bearer_on_disk, fresh_id
from utils.harness import process_group_options, stop_process

pytestmark = [pytest.mark.live, pytest.mark.copilot]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_listener(port: int, bridge: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert bridge.poll() is None, f"ug vscode-bridge exited with {bridge.returncode}"
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    pytest.fail(f"ug vscode-bridge did not listen on port {port} within {timeout}s")


def _post(port: int, path: str, body: dict, headers: dict[str, str]) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(body),
            headers={"Content-Type": "application/json", **headers},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def test_ug_vscode_bridge_serves_a_model_turn_with_only_its_secret(live_session, workspace):
    """Scenario: configure Copilot, start `ug vscode-bridge` for the workspace, then send a
    Claude turn the way a VS Code Custom Endpoint model does: Anthropic Messages to the bridge,
    with the bridge secret as its only credential.

    Expected: the bridge answers with a real model reply containing a fresh value; a wrong
    secret gets a 401; no file under the session home holds the Databricks credential. VS Code
    itself is not launched here.
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
    host = session.state()["current_workspace"]
    port = _free_port()
    stderr_path = session.artifacts / "vscode-bridge-stderr.txt"
    with stderr_path.open("w", encoding="utf-8") as stderr:
        bridge = subprocess.Popen(
            [str(session.binary), "vscode-bridge", "--host", host, "--port", str(port)],
            cwd=session.cwd,
            env=session.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            **process_group_options(),
        )
        try:
            _wait_for_listener(port, bridge)
            secret_file = session.home / ".ucode/vscode-bridge.json"
            secret = json.loads(secret_file.read_text(encoding="utf-8"))["secret"]
            value = fresh_id()
            turn = {
                "model": CLAUDE_TEST_MODEL,
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Reply with exactly this word and nothing else: {value}",
                    }
                ],
            }
            messages = "/ai-gateway/anthropic/v1/messages"
            anthropic = {"anthropic-version": "2023-06-01"}

            status, reply = _post(port, messages, turn, {**anthropic, "x-api-key": secret})
            wrong_status, wrong = _post(port, messages, turn, {**anthropic, "x-api-key": "wrong"})
        finally:
            stop_process(bridge)
    session.record(
        "vscode-bridge.json",
        {"model": CLAUDE_TEST_MODEL, "statuses": [status, wrong_status]},
    )

    assert status == 200, reply
    text = "".join(block.get("text", "") for block in reply.get("content", []))
    assert value in text, reply
    assert wrong_status == 401, wrong
    assert_no_bearer_on_disk(session)
