"""Verify the agent fetched the live token and then stopped the server."""

import hashlib
import os
import socket
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def main() -> None:
    state = APP / ".server_state"
    assert (state / "port").exists(), "server.py was never started"
    token = (APP / "token.txt").read_text().strip()
    want = (state / "token.sha256").read_text().strip()
    assert hashlib.sha256(token.encode()).hexdigest() == want, "token.txt does not match"

    port = int((state / "port").read_text())
    with socket.socket() as sock:
        sock.settimeout(2)
        assert sock.connect_ex(("127.0.0.1", port)) != 0, "server is still running"
    print("ok")


if __name__ == "__main__":
    main()
