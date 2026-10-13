"""Component coverage for concurrent requests through the real stdio dispatcher."""

from __future__ import annotations

import io
import json
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib import error as urllib_error
from urllib.request import Request

import pytest

from ucode import mcp_web_search

WAIT_SECONDS = 5
HTTP_DEADLOCK_SECONDS = 15


@dataclass
class HttpGate:
    failure: Exception | None = None
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


class ControlledHttp:
    def __init__(self) -> None:
        self.gates: dict[str, HttpGate] = {}
        self.calls: list[str] = []
        self.active = 0
        self.peak_active = 0
        self.lock = threading.Lock()

    def add(
        self, query: str, *, blocked: bool = True, failure: Exception | None = None
    ) -> HttpGate:
        gate = HttpGate(failure=failure)
        if not blocked:
            gate.release.set()
        self.gates[query] = gate
        return gate

    def release_all(self) -> None:
        for gate in self.gates.values():
            gate.release.set()

    def open(self, request: Request, timeout: float) -> io.BytesIO:
        assert request.data is not None
        query = json.loads(request.data)["input"][0]["content"]
        gate = self.gates[query]
        with self.lock:
            self.calls.append(query)
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        gate.started.set()
        try:
            # Bound even a broken dispatcher without letting a timeout prove concurrency.
            assert gate.release.wait(HTTP_DEADLOCK_SECONDS), f"HTTP gate timed out: {query}"
            if gate.failure is not None:
                raise gate.failure
            return io.BytesIO(
                json.dumps(
                    {
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": f"answer:{query}"}],
                            }
                        ]
                    }
                ).encode()
            )
        finally:
            with self.lock:
                self.active -= 1
            gate.finished.set()


class LineInput:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.eof_read = threading.Event()
        self.closed = False

    def send(self, payload: dict[str, Any]) -> None:
        assert not self.closed
        self.lines.put(json.dumps(payload) + "\n")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.lines.put(None)

    def __iter__(self) -> Iterator[str]:
        while (line := self.lines.get()) is not None:
            yield line
        self.eof_read.set()


class ResponseOutput:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.text = ""

    def write(self, text: str) -> int:
        with self.condition:
            self.text += text
            self.condition.notify_all()
        return len(text)

    def flush(self) -> None:
        pass

    def responses(self) -> list[dict[str, Any]]:
        with self.condition:
            return [json.loads(line) for line in self.text.splitlines()]

    def wait_for(self, request_id: str | int) -> dict[str, Any]:
        with self.condition:
            assert self.condition.wait_for(
                lambda: any(response.get("id") == request_id for response in self.responses()),
                timeout=WAIT_SECONDS,
            ), f"No response for {request_id!r}; received {self.responses()!r}"
            matches = [
                response for response in self.responses() if response.get("id") == request_id
            ]
            assert len(matches) == 1
            return matches[0]


class RunningServer:
    def __init__(self, http: ControlledHttp) -> None:
        self.http = http
        self.stdin = LineInput()
        self.stdout = ResponseOutput()
        self.error: BaseException | None = None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            mcp_web_search.serve(stdin=self.stdin, stdout=self.stdout)
        except BaseException as exc:
            # A dispatcher exception must fail the owning test, not disappear in a thread.
            self.error = exc
        finally:
            self.done.set()

    def request(self, request_id: str | int, method: str, **params: Any) -> None:
        self.stdin.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

    def search(self, request_id: str | int, query: str) -> None:
        self.request(request_id, "tools/call", name="web_search", arguments={"query": query})

    def cancel(self, request_id: str | int) -> None:
        self.stdin.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id, "reason": "test cancellation"},
            }
        )

    def catalog(self, request_id: str | int) -> None:
        # A response proves the dispatcher consumed every preceding input line.
        self.request(request_id, "tools/list")
        response = self.stdout.wait_for(request_id)
        assert [tool["name"] for tool in response["result"]["tools"]] == ["web_search"]

    def answer(self, request_id: str | int, query: str) -> None:
        assert self.stdout.wait_for(request_id) == {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": f"answer:{query}"}]},
        }

    def finish(self) -> None:
        self.stdin.close()
        self.thread.join(WAIT_SECONDS)
        assert not self.thread.is_alive(), "serve() did not finish after EOF"
        assert self.error is None, f"serve() failed: {self.error!r}"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[RunningServer]:
    http = ControlledHttp()
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("UCODE_WEB_SEARCH_MODEL", "search-model")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    # Replace only external auth and HTTP; registration, dispatch and decoding remain real.
    monkeypatch.setattr(mcp_web_search, "get_databricks_token", lambda *_: "test-token")
    monkeypatch.setattr(mcp_web_search.urllib_request, "urlopen", http.open)
    running = RunningServer(http)
    running.thread.start()
    try:
        running.request("initialize", "initialize")
        assert running.stdout.wait_for("initialize")["result"]["serverInfo"]["name"] == (
            "ucode-web-search"
        )
        running.stdin.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        yield running
    finally:
        http.release_all()
        running.finish()


def test_fast_search_and_catalog_finish_while_slow_http_is_blocked(server: RunningServer) -> None:
    slow = server.http.add("slow")
    server.http.add("fast", blocked=False)
    server.search(1, "slow")
    assert slow.started.wait(WAIT_SECONDS)
    server.search("fast", "fast")
    server.catalog("catalog")
    server.answer("fast", "fast")
    assert not slow.finished.is_set()
    assert not any(response["id"] == 1 for response in server.stdout.responses())
    slow.release.set()
    server.answer(1, "slow")
    server.finish()
    ids = [response["id"] for response in server.stdout.responses()]
    assert ids.index("fast") < ids.index(1)
    assert len(ids) == len(set(ids)) == 4


@pytest.mark.parametrize("upstream_failure", [False, True])
def test_active_cancellation_retains_worker_until_http_finishes(
    server: RunningServer, upstream_failure: bool
) -> None:
    gates = [
        server.http.add(
            f"active-{index}",
            failure=(
                urllib_error.URLError("upstream failed")
                if upstream_failure and index == 0
                else None
            ),
        )
        for index in range(4)
    ]
    fifth = server.http.add("fifth", blocked=False)
    for index in range(4):
        server.search(index, f"active-{index}")
    for gate in gates:
        assert gate.started.wait(WAIT_SECONDS)
    server.search(4, "fifth")
    server.catalog("queued")
    assert not fifth.started.is_set()
    assert server.http.peak_active == 4

    server.cancel(0)
    server.catalog("cancelled")
    assert not gates[0].finished.is_set()
    assert not fifth.started.is_set()
    gates[0].release.set()
    server.answer(4, "fifth")
    server.http.release_all()
    server.finish()
    for index in range(1, 4):
        server.answer(index, f"active-{index}")
    assert server.http.peak_active == 4
    ids = [response["id"] for response in server.stdout.responses()]
    assert set(ids) == {"initialize", "queued", "cancelled", 1, 2, 3, 4}
    assert len(ids) == len(set(ids))


def test_queued_cancellation_never_enters_http(server: RunningServer) -> None:
    gates = [server.http.add(f"active-{index}") for index in range(4)]
    cancelled = server.http.add("cancelled", blocked=False)
    server.http.add("survivor", blocked=False)
    for index in range(4):
        server.search(index, f"active-{index}")
    for gate in gates:
        assert gate.started.wait(WAIT_SECONDS)
    server.search(4, "cancelled")
    server.search(5, "survivor")
    server.catalog("queued")
    server.cancel(4)
    server.catalog("cancelled")
    gates[0].release.set()
    server.answer(5, "survivor")
    server.http.release_all()
    server.finish()
    assert not cancelled.started.is_set()
    assert "cancelled" not in server.http.calls
    for index in range(4):
        server.answer(index, f"active-{index}")
    ids = [response["id"] for response in server.stdout.responses()]
    assert set(ids) == {"initialize", "queued", "cancelled", 0, 1, 2, 3, 5}
    assert len(ids) == len(set(ids))


def test_malformed_and_unknown_cancellations_leave_active_requests_intact(
    server: RunningServer,
) -> None:
    gates = [server.http.add(f"active-{index}") for index in range(2)]
    for index, gate in enumerate(gates):
        server.search(index, f"active-{index}")
        assert gate.started.wait(WAIT_SECONDS)
    for params in (
        None,
        [],
        "invalid",
        {},
        {"requestId": None},
        {"requestId": {}},
        {"requestId": []},
        {"requestId": True},
        {"requestId": False},
        {"requestId": 999},
        {"requestId": "missing"},
    ):
        server.stdin.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params})
    server.catalog("after-cancellation")
    server.http.release_all()
    server.finish()
    for index in range(2):
        server.answer(index, f"active-{index}")
    ids = [response["id"] for response in server.stdout.responses()]
    assert set(ids) == {"initialize", "after-cancellation", 0, 1}
    assert len(ids) == len(set(ids))


def test_notifications_do_not_start_http_or_emit_responses(server: RunningServer) -> None:
    server.http.add("notification", blocked=False)
    server.stdin.send(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "web_search", "arguments": {"query": "notification"}},
        }
    )
    server.stdin.send({"jsonrpc": "2.0", "method": "tools/list"})
    server.stdin.send({"jsonrpc": "2.0", "method": "notifications/unknown"})
    server.catalog("after-notifications")
    server.finish()
    assert server.http.calls == []
    assert [response["id"] for response in server.stdout.responses()] == [
        "initialize",
        "after-notifications",
    ]


@pytest.mark.parametrize("unexpected", [False, True])
def test_worker_error_does_not_interrupt_other_requests(
    server: RunningServer, unexpected: bool
) -> None:
    failure = (
        ValueError("unexpected worker failure")
        if unexpected
        else urllib_error.URLError("upstream failed")
    )
    server.http.add("failed", blocked=False, failure=failure)
    server.http.add("success", blocked=False)
    server.search(1, "failed")
    server.search(2, "success")
    response = server.stdout.wait_for(1)
    if unexpected:
        assert response == {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "Internal error"},
        }
    else:
        assert response["result"] == {
            "content": [{"type": "text", "text": "Responses API request failed: upstream failed"}],
            "isError": True,
        }
    server.answer(2, "success")
    server.catalog("after-error")
    server.finish()
    ids = [response["id"] for response in server.stdout.responses()]
    assert set(ids) == {"initialize", "after-error", 1, 2}
    assert len(ids) == len(set(ids))


def test_worker_output_failure_stops_dispatch_and_cancels_queued_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_failure = BrokenPipeError("client closed response pipe")
    queued_cancelled = threading.Event()

    class FailingOutput(ResponseOutput):
        def __init__(self) -> None:
            super().__init__()
            self.fail_writes = threading.Event()

        def write(self, text: str) -> int:
            if self.fail_writes.is_set():
                raise output_failure
            return super().write(text)

        def flush(self) -> None:
            pass

    original_cancel = mcp_web_search.Future.cancel

    def observe_cancel(future: Any) -> bool:
        cancelled = original_cancel(future)
        if cancelled:
            queued_cancelled.set()
        return cancelled

    monkeypatch.setattr(mcp_web_search.Future, "cancel", observe_cancel)
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("UCODE_WEB_SEARCH_MODEL", "search-model")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr(mcp_web_search, "get_databricks_token", lambda *_: "test-token")
    http = ControlledHttp()
    monkeypatch.setattr(mcp_web_search.urllib_request, "urlopen", http.open)
    active = [http.add(f"active-{index}") for index in range(4)]
    queued = http.add("queued", blocked=False)
    rejected = http.add("rejected", blocked=False)
    running = RunningServer(http)
    failing_output = FailingOutput()
    running.stdout = failing_output
    running.thread.start()
    try:
        for index in range(4):
            running.search(index, f"active-{index}")
        for gate in active:
            assert gate.started.wait(WAIT_SECONDS)
        running.search(4, "queued")
        running.catalog("queued-confirmation")

        failing_output.fail_writes.set()
        active[0].release.set()
        assert queued_cancelled.wait(WAIT_SECONDS), "output failure did not cancel queued work"
        running.search(5, "rejected")

        http.release_all()
        running.thread.join(WAIT_SECONDS)
        assert not running.thread.is_alive()
        assert running.error is output_failure
        assert not queued.started.is_set()
        assert not rejected.started.is_set()
        assert set(http.calls) == {f"active-{index}" for index in range(4)}
    finally:
        http.release_all()
        running.stdin.close()
        running.thread.join(WAIT_SECONDS)
        assert not running.thread.is_alive(), "serve() leaked its worker shutdown thread"


def test_eof_drains_active_and_queued_searches(server: RunningServer) -> None:
    gates = [server.http.add(f"query-{index}") for index in range(6)]
    for index in range(6):
        server.search(index, f"query-{index}")
    for gate in gates[:4]:
        assert gate.started.wait(WAIT_SECONDS)
    server.catalog("queued")
    assert not any(gate.started.is_set() for gate in gates[4:])
    server.stdin.close()
    assert server.stdin.eof_read.wait(WAIT_SECONDS)
    assert not server.done.is_set()
    for gate in gates[:4]:
        gate.release.set()
    for gate in gates[4:]:
        assert gate.started.wait(WAIT_SECONDS)
    assert not server.done.is_set()
    server.http.release_all()
    server.finish()
    for index in range(6):
        server.answer(index, f"query-{index}")
    ids = [response["id"] for response in server.stdout.responses()]
    assert len(ids) == len(set(ids)) == 8
    assert server.http.peak_active == 4


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt])
def test_input_failure_cancels_queued_searches(
    monkeypatch: pytest.MonkeyPatch, failure_type: type[BaseException]
) -> None:
    input_failure = failure_type("input stream failed")

    class FailingInput(LineInput):
        def __iter__(self) -> Iterator[str]:
            yield from super().__iter__()
            raise input_failure

    joining_workers = threading.Event()
    original_shutdown = mcp_web_search.ThreadPoolExecutor.shutdown

    def observe_shutdown(executor: Any, wait: bool = True, *, cancel_futures: bool = False) -> None:
        # Releasing HTTP on the input exception races its cleanup. Final joining
        # starts after cleanup, or before draining queued work on the old code.
        if wait:
            joining_workers.set()
        original_shutdown(executor, wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(mcp_web_search.ThreadPoolExecutor, "shutdown", observe_shutdown)
    http = ControlledHttp()
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("UCODE_WEB_SEARCH_MODEL", "search-model")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr(mcp_web_search, "get_databricks_token", lambda *_: "test-token")
    monkeypatch.setattr(mcp_web_search.urllib_request, "urlopen", http.open)
    gates = [http.add(f"active-{index}") for index in range(4)]
    queued = http.add("queued", blocked=False)
    running = RunningServer(http)
    running.stdin = FailingInput()
    running.thread.start()
    try:
        running.request("initialize", "initialize")
        assert running.stdout.wait_for("initialize")["result"]["serverInfo"]["name"] == (
            "ucode-web-search"
        )
        running.stdin.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for index in range(4):
            running.search(index, f"active-{index}")
        for gate in gates:
            assert gate.started.wait(WAIT_SECONDS)
        running.search(4, "queued")
        running.catalog("queued")
        assert not queued.started.is_set()

        running.stdin.close()
        assert joining_workers.wait(WAIT_SECONDS), "serve() did not begin final worker joining"
        assert not running.done.is_set()
        http.release_all()
        running.thread.join(WAIT_SECONDS)
        assert not running.thread.is_alive()
        assert running.error is input_failure
        assert all(gate.finished.is_set() for gate in gates)
        assert not queued.started.is_set()
        assert set(http.calls) == {f"active-{index}" for index in range(4)}
        assert [response["id"] for response in running.stdout.responses()] == [
            "initialize",
            "queued",
        ]
    finally:
        http.release_all()
        running.stdin.close()
        running.thread.join(WAIT_SECONDS)
        assert not running.thread.is_alive(), "serve() leaked its worker shutdown thread"


def test_interruption_during_eof_drain_cancels_queued_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt("interrupted while draining EOF")
    joining_workers = threading.Event()
    original_shutdown = mcp_web_search.ThreadPoolExecutor.shutdown
    executors: list[Any] = []
    injected = False

    def interrupt_first_drain(
        executor: Any, wait: bool = True, *, cancel_futures: bool = False
    ) -> None:
        nonlocal injected
        if executor not in executors:
            executors.append(executor)
        if wait and not cancel_futures and not injected:
            injected = True
            raise interruption
        if wait:
            joining_workers.set()
        original_shutdown(executor, wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(mcp_web_search.ThreadPoolExecutor, "shutdown", interrupt_first_drain)
    http = ControlledHttp()
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("UCODE_WEB_SEARCH_MODEL", "search-model")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr(mcp_web_search, "get_databricks_token", lambda *_: "test-token")
    monkeypatch.setattr(mcp_web_search.urllib_request, "urlopen", http.open)
    gates = [http.add(f"active-{index}") for index in range(4)]
    queued = http.add("queued", blocked=False)
    running = RunningServer(http)
    running.thread.start()
    try:
        running.request("initialize", "initialize")
        assert running.stdout.wait_for("initialize")["result"]["serverInfo"]["name"] == (
            "ucode-web-search"
        )
        running.stdin.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for index in range(4):
            running.search(index, f"active-{index}")
        for gate in gates:
            assert gate.started.wait(WAIT_SECONDS)
        running.search(4, "queued")
        running.catalog("queued")
        assert not queued.started.is_set()

        running.stdin.close()
        assert running.stdin.eof_read.wait(WAIT_SECONDS)
        assert joining_workers.wait(WAIT_SECONDS), (
            "serve() did not resume joining after interruption"
        )
        assert injected
        assert not running.done.is_set()
        http.release_all()
        running.thread.join(WAIT_SECONDS)
        assert not running.thread.is_alive()
        assert running.error is interruption
        assert all(gate.finished.is_set() for gate in gates)
        assert not queued.started.is_set()
        assert set(http.calls) == {f"active-{index}" for index in range(4)}
        assert [response["id"] for response in running.stdout.responses()] == [
            "initialize",
            "queued",
        ]
    finally:
        # The pre-fix server exits before invoking real shutdown; reclaim its pool too.
        for executor in tuple(executors):
            original_shutdown(executor, wait=False, cancel_futures=True)
        http.release_all()
        running.stdin.close()
        running.thread.join(WAIT_SECONDS)
        for executor in tuple(executors):
            original_shutdown(executor, wait=True, cancel_futures=True)
        assert not running.thread.is_alive(), "serve() leaked its worker shutdown thread"
