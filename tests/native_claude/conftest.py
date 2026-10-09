"""Direct fixture API for native component tests; no gateway or live inference."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def fixture_api():
    captures = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path.split("?")[0].endswith("count_tokens"):
                raw = b'{"input_tokens":1}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            last = body.get("messages", [{}])[-1].get("content", "")
            child = "CHILD_SETTINGS_PROBE" in json.dumps(last)
            source = "child" if child else "parent"
            captures.append(
                {
                    "session": self.path.split("/")[1],
                    "source": source,
                    "value": body.get("smart_router_recipe_name", "ABSENT"),
                    "caller": body.get("caller_field"),
                }
            )
            tool_result = isinstance(last, list) and any(
                isinstance(item, dict) and item.get("type") == "tool_result" for item in last
            )
            if not child and not tool_result:
                content = {
                    "type": "tool_use",
                    "id": f"tool_probe_{len(captures)}",
                    "name": "Agent",
                    "input": {},
                }
                delta = {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(
                        {
                            "description": "Check child payload",
                            "prompt": "Reply CHILD_SETTINGS_PROBE complete",
                            "subagent_type": "general-purpose",
                        }
                    ),
                }
                stop = "tool_use"
            else:
                content = {"type": "text", "text": ""}
                delta = {
                    "type": "text_delta",
                    "text": "child complete" if child else "parent complete",
                }
                stop = "end_turn"
            message = {
                "id": f"msg_probe_{len(captures)}",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            }
            events = [
                ("message_start", {"type": "message_start", "message": message}),
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": content},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": delta},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": 2},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            raw = "".join(
                f"event: {name}\ndata: {json.dumps(event)}\n\n" for name, event in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", captures
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
