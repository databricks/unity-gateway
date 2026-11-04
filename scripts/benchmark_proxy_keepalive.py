"""Real loopback connections with an explicit per-connection setup delay.

Run with ``uv run scripts/benchmark_proxy_keepalive.py``; ``--source`` selects a
baseline checkout using the same dependencies. Default runtime is about 36 seconds.
This measures request time after idle periods and counts connections. The explicit
server delay models connection setup cost; it is not a measured TLS or gateway RTT.
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--runs", type=int, default=6)
    parser.add_argument("--idle-seconds", type=float, default=6)
    parser.add_argument("--connection-delay-ms", type=float, default=50)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    sys.path.insert(0, args.source + "/src")
    import httpx

    from ucode.gateway_proxy import start_relay_proxy

    accepted = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True

        def setup(self):
            super().setup()
            accepted.append(self.client_address)
            time.sleep(args.connection_delay_ms / 1000)

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy, cache, client = start_relay_proxy(
        f"http://127.0.0.1:{upstream.server_port}", lambda _: "dummy", 0
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    samples = []
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{proxy.server_port}", timeout=10
        ) as downstream:
            assert downstream.get("/v1/models").content == b"ok"
            for _ in range(args.runs):
                time.sleep(args.idle_seconds)
                count = len(accepted)
                start = time.perf_counter()
                assert downstream.get("/v1/models").content == b"ok"
                samples.append(
                    {
                        "elapsed_ms": (time.perf_counter() - start) * 1000,
                        "new_upstream_connections": len(accepted) - count,
                    }
                )
    finally:
        proxy.shutdown()
        proxy.server_close()
        cache.stop()
        client.close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)
    print(
        json.dumps(
            {
                "idle_seconds": args.idle_seconds,
                "simulated_connection_setup_ms": args.connection_delay_ms,
                "samples": samples,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
