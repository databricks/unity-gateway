"""Forward a native model-setting receipt to this launch's PTY wrapper."""

import json
import socket
import sys
import time


def main() -> None:
    request = json.loads(sys.stdin.read(65536))
    if not isinstance(request, dict):
        raise ValueError("Expected a model-setting receipt")
    request["method"] = "model_setting"
    # A request can expire while waiting behind another connection in the socket backlog.
    request["deadline"] = time.monotonic() + 4.0
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5.0)
        client.connect(sys.argv[1])
        client.sendall((json.dumps(request) + "\n").encode())
        with client.makefile("rb") as stream:
            response = json.loads(stream.readline(65536))
    sys.stdout.write(json.dumps(response))


if __name__ == "__main__":
    main()
