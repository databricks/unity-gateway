"""Controlled Messages responses for testing the real Claude request/usage boundary."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _stream(reply: dict, model: str, index: int) -> bytes:
    usage = reply["usage"]
    events = [
        {
            "type": "message_start",
            "message": {
                "id": f"msg_fixture_{index}",
                "type": "message",
                "role": "assistant",
                "model": reply.get("model", model),
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {**usage, "output_tokens": 0},
            },
        }
    ]
    for i, block in enumerate(reply["content"]):
        is_tool = block["type"] == "tool_use"
        start = {**block, "input": {}} if is_tool else {**block, "text": ""}
        delta = (
            {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            if is_tool
            else {"type": "text_delta", "text": block["text"]}
        )
        events.extend(
            [
                {"type": "content_block_start", "index": i, "content_block": start},
                {"type": "content_block_delta", "index": i, "delta": delta},
                {"type": "content_block_stop", "index": i},
            ]
        )
    events.extend(
        [
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "tool_use"
                    if any(block["type"] == "tool_use" for block in reply["content"])
                    else "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": usage["output_tokens"]},
            },
            {"type": "message_stop"},
        ]
    )
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


@contextmanager
def messages_api(replies: list[dict]) -> Iterator[tuple[str, list[dict]]]:
    requests: list[dict] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
            if self.path.split("?")[0] != "/v1/messages":
                self.send_error(404)
                return
            with lock:
                index = len(requests)
                requests.append(request)
            if index >= len(replies):
                self.send_error(500, "Unexpected model request")
                return
            body = _stream(replies[index], request["model"], index)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
