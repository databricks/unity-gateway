"""Token service. Run it in the background; it stops itself after 15 minutes."""

import hashlib
import http.server
import secrets
import threading
from pathlib import Path

STATE = Path(__file__).resolve().parent / ".server_state"
TOKEN = secrets.token_hex(16)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/token":
            self.send_error(404)
        elif self.headers.get("X-Bench-Client") != "ug":
            self.send_error(403, "missing X-Bench-Client: ug header")
        else:
            body = TOKEN.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    STATE.mkdir(exist_ok=True)
    (STATE / "token.sha256").write_text(hashlib.sha256(TOKEN.encode()).hexdigest())
    (STATE / "port").write_text(str(server.server_address[1]))
    print(f"listening on 127.0.0.1:{server.server_address[1]}", flush=True)
    threading.Timer(900, server.shutdown).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
