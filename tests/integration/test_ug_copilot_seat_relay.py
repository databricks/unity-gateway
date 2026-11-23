"""CUJ: `ug copilot` on a Model Provider Service that relays the developer's GitHub Copilot seat."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from utils.constants import COPILOT_HEADLESS_FLAGS
from utils.evidence import FileTask, assert_no_bearer_on_disk, assert_no_session_registry_left

pytestmark = [pytest.mark.live, pytest.mark.copilot, pytest.mark.copilot_seat]


class _Recorder(BaseHTTPRequestHandler):
    """Stands in for a proxy named in the environment: records what reaches it and refuses it."""

    requests: list[str] = []

    def _record(self):
        self.requests.append(f"{self.command} {self.path}")
        self.send_response(502)
        self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_CONNECT = _record

    def log_message(self, *args):
        return None


def _recording_proxy() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _assert_token_not_under_ug_files(session, token: str) -> None:
    encoded = token.encode()
    for path in (session.home / ".ucode").rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert encoded not in path.read_bytes(), f"{path} holds the GitHub token"


def test_ug_copilot_seat_relay_headless_task(
    live_session, workspace, copilot_seat_provider, copilot_seat_github_token
):
    """Scenario: configure Copilot on a seat-relay provider, then run a headless prompt through
    `ug copilot` with a plain HTTP proxy named in the environment and no loopback exception.

    Expected: Copilot, signed in to GitHub through GH_TOKEN, completes the file task through the
    relay; the proxy receives nothing, because the launch environment exempts loopback from it;
    neither the Databricks bearer nor the GitHub token lands in ug's files, and the session
    registry is gone afterwards.
    """
    session = live_session
    proxy = _recording_proxy()
    _Recorder.requests.clear()
    try:
        session.env["HTTP_PROXY"] = f"http://127.0.0.1:{proxy.server_address[1]}"
        session.env.pop("NO_PROXY", None)
        session.env.pop("no_proxy", None)
        session.env["GH_TOKEN"] = copilot_seat_github_token
        session.run(
            "configure",
            "--agents",
            "copilot",
            "--provider",
            copilot_seat_provider,
            "--workspace",
            workspace,
            "--skip-validate",
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )
        task = FileTask(session)
        result = session.run("copilot", "-p", task.prompt, *COPILOT_HEADLESS_FLAGS, timeout=240)
    finally:
        proxy.shutdown()
        proxy.server_close()

    task.assert_headless_answer("copilot", result)
    session.record("seat-relay-proxy-requests.json", list(_Recorder.requests))
    assert _Recorder.requests == [], _Recorder.requests
    assert_no_bearer_on_disk(session)
    _assert_token_not_under_ug_files(session, copilot_seat_github_token)
    assert_no_session_registry_left(session)
