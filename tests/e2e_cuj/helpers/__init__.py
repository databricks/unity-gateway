"""Black-box helpers for dedicated-workspace CUJs.

This module talks to the installed commands and the Databricks SDK only.  It deliberately does
not import ``ucode``: a CUJ must exercise the installed wheel just as a user does.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CALL_TYPES = {
    "custom_tool_call",
    "function_call",
    "mcp_tool_call",
    "tool_call",
    "tool_use",
}
_RESULT_TYPES = {
    "custom_tool_call_output",
    "function_call_output",
    "mcp_tool_result",
    "mcp_tool_call_output",
    "tool_result",
    "tool_call_output",
}


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    elif os.name == "nt":
        process.kill()
    process.wait(timeout=5)


def _process_group_options() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


def _json_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _json_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _json_strings(item)]
    return []


def _dict_nodes(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _dict_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dict_nodes(child)


def _first_string(mapping: dict, *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _call_id(mapping: dict) -> str | None:
    return _first_string(mapping, "id", "call_id", "callId", "tool_use_id", "toolUseId")


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CujSession:
    """An isolated home/project and subprocess runner for one CUJ case."""

    def __init__(self, run_root: Path, binary: Path, case_name: str):
        self._temporary = tempfile.TemporaryDirectory(prefix="cuj-", dir=run_root)
        self.root = Path(self._temporary.name)
        self.home = self.root / "home"
        self.project = self.root / "project with spaces"
        self.home.mkdir()
        self.project.mkdir()
        self.binary = binary
        self.artifacts = run_root / "artifacts" / case_name
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self._commands = 0
        self.env = self._environment()

    def _environment(self) -> dict[str, str]:
        keep = (
            "PATH",
            "SYSTEMROOT",
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
        )
        env = {key: os.environ[key] for key in keep if key in os.environ}
        bearer = os.environ.get("DATABRICKS_BEARER", "").strip()
        if not bearer:
            raise AssertionError("DATABRICKS_BEARER must be provided by the CUJ runner")
        env.update(
            {
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_CACHE_HOME": str(self.home / ".cache"),
                "XDG_DATA_HOME": str(self.home / ".local/share"),
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "CODEX_HOME": str(self.home / ".codex"),
                "DATABRICKS_CONFIG_FILE": str(self.home / ".databrickscfg"),
                "DATABRICKS_BEARER": bearer,
                "UG_INTEGRATION_BIN": str(self.binary),
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "NO_COLOR": "1",
                "TERM": "dumb",
                "COLUMNS": "160",
                "ENABLE_SMART_ROUTING_V2": "0",
                "PYTHONNOUSERSITE": "1",
            }
        )
        return env

    def _redact(self, text: str) -> str:
        for name in ("DATABRICKS_BEARER", "UG_CUJ_SP_CLIENT_SECRET"):
            token = os.environ.get(name) or self.env.get(name)
            if token:
                text = text.replace(token, "<redacted>")
        return _ANSI.sub("", text)

    def record(self, name: str, value: object) -> None:
        payload = json.dumps(value, indent=2, sort_keys=True, default=str)
        self.artifacts.joinpath(name).write_text(self._redact(payload), encoding="utf-8")

    def run(
        self,
        *args: str,
        timeout: int = 180,
        binary: str | Path | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        command = tuple(str(binary or self.binary) for _ in [0]) + tuple(args)
        process = subprocess.Popen(
            list(command),
            cwd=self.project,
            env=self.env,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_process_group_options(),
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(process)
            stdout, stderr = process.communicate(timeout=5)
        finally:
            _kill_process_group(process)
        result = CommandResult(
            command,
            process.returncode,
            self._redact(stdout),
            self._redact(stderr),
            timed_out,
        )
        self._commands += 1
        self.record(
            f"command-{self._commands}.json",
            {
                "argv": command,
                "returncode": result.returncode,
                "stderr": result.stderr,
                "stdout": result.stdout,
                "timed_out": result.timed_out,
            },
        )
        if result.timed_out:
            raise AssertionError(f"Command exceeded {timeout}s: {command}")
        if check and result.returncode != 0:
            raise AssertionError(
                f"Command failed ({result.returncode}): {command}\n{result.stderr}"
            )
        return result

    def external(self, program: str, *args: str, timeout: int = 120) -> CommandResult:
        return self.run(*args, binary=program, timeout=timeout)

    def _record_tui_artifact(
        self,
        command: tuple[str, ...],
        *,
        stdout: str,
        returncode: int | None,
        timed_out: bool,
        actions: list[dict[str, str]],
        screen: str,
        failure: BaseException | None,
    ) -> None:
        self._commands += 1
        try:
            self.record(
                f"command-{self._commands}.json",
                {
                    "argv": command,
                    "returncode": returncode,
                    "stderr": "",
                    "stdout": stdout,
                    "timed_out": timed_out,
                    "interactive": True,
                    "actions": actions,
                    "screen": screen,
                    "error": f"{type(failure).__name__}: {failure}" if failure else None,
                },
            )
        except BaseException:
            if failure is None:
                raise

    def run_tui(
        self,
        agent: str,
        prompt: str,
        *,
        completion: Callable[[CujSession], bool],
        timeout: int = 240,
    ) -> CommandResult:
        """Run a real interactive agent through a PTY and return its terminal transcript."""
        if os.name != "posix":
            raise AssertionError("The dedicated CUJ requires POSIX PTY support.")
        try:
            pexpect = importlib.import_module("pexpect")
            pyte = importlib.import_module("pyte")
        except ImportError as exc:
            raise AssertionError("The dedicated CUJ runner must install pexpect and pyte.") from exc

        if agent not in {"claude", "codex"}:
            raise AssertionError(f"Unsupported TUI agent: {agent}")
        # Bare ug resolves the workspace default (Codex in CUJ2); Claude needs its explicit
        # subcommand so this path exercises the configured Claude provider too.
        command = (str(self.binary),) if agent == "codex" else (str(self.binary), "claude")
        env = {**self.env, "TERM": "xterm-256color", "COLUMNS": "140", "LINES": "60"}
        try:
            child = pexpect.spawn(
                command[0],
                command[1:],
                cwd=str(self.project),
                env=env,
                encoding="utf-8",
                codec_errors="replace",
                dimensions=(60, 140),
                timeout=1,
            )
        except BaseException as exc:
            self._record_tui_artifact(
                command,
                stdout="",
                returncode=None,
                timed_out=False,
                actions=[],
                screen="",
                failure=exc,
            )
            raise

        def send_terminal_reply(data: str) -> None:
            child.send(data)

        # pyte's screen emits terminal-device replies through this stream hook. The callback is
        # deliberately limited to replies generated from the rendered screen state.
        class TerminalScreen(pyte.Screen):
            def write_process_input(self, data: str) -> None:
                send_terminal_reply(data)

        screen = TerminalScreen(140, 60)
        stream = pyte.Stream(screen)
        output: list[str] = []
        actions: list[dict[str, str]] = []
        ended = False
        timed_out = False

        def visible() -> str:
            return "\n".join(line.rstrip() for line in screen.display)

        def read() -> None:
            nonlocal ended
            try:
                chunk = child.read_nonblocking(size=65536, timeout=0.2)
            except pexpect.TIMEOUT:
                return
            except pexpect.EOF:
                before = child.before
                if before:
                    output.append(before)
                    stream.feed(before)
                ended = True
                return
            output.append(chunk)
            stream.feed(chunk)

        def send(keys: str, reason: str) -> None:
            actions.append({"reason": reason, "keys": keys, "screen_before": visible()})
            child.send(keys)

        def selected_line() -> str:
            return next(
                (line.strip() for line in visible().splitlines() if re.match(r"^\s*[›❯>]", line)),
                "",
            )

        def choose(prompt_text: str, label: str) -> None:
            nonlocal timed_out
            deadline = time.monotonic() + 120
            visited: set[str] = set()
            while time.monotonic() < deadline:
                read()
                current_screen = visible()
                assert not ended, f"TUI exited while waiting for {prompt_text}:\n{current_screen}"
                current = selected_line()
                if prompt_text in current_screen and current:
                    if label in current:
                        send("\r", f"choose {label}")
                        return
                    assert current not in visited, f"Menu does not offer {label}:\n{current_screen}"
                    visited.add(current)
                    send("\x1b[B", f"move towards {label}")
                time.sleep(0.05)
            timed_out = True
            raise AssertionError(f"TUI did not show {prompt_text} within 120s:\n{visible()}")

        def wait_for_prompt() -> None:
            nonlocal timed_out
            deadline = time.monotonic() + 120
            handled: set[str] = set()
            ready_since: float | None = None
            while time.monotonic() < deadline:
                read()
                assert not ended, f"TUI exited before its prompt:\n{visible()}"
                current_screen = visible()
                if "Accessing workspace:" in current_screen and self.project.name in current_screen:
                    choose("Accessing workspace:", "Yes, I trust this folder")
                    continue
                if "Hooks need review" in current_screen:
                    choose("Hooks need review", "Trust all and continue")
                    continue
                dialogs = [
                    (
                        "theme",
                        "Choose the text style" in current_screen and "Dark mode" in current_screen,
                    ),
                    (
                        "security-notes",
                        "Security notes" in current_screen
                        and "Enter to continue" in current_screen,
                    ),
                    (
                        "trust-folder",
                        self.project.name in current_screen
                        and bool(
                            re.search(r"1[.)]\s+Yes, I trust (?:this|the) folder", current_screen)
                        ),
                    ),
                    (
                        "trust-directory",
                        self.project.name in current_screen
                        and "trust" in current_screen.lower()
                        and bool(re.search(r"1[.)]\s+Yes, (?:continue|proceed)", current_screen)),
                    ),
                ]
                handled_dialog = False
                for label, shown in dialogs:
                    if shown:
                        handled_dialog = True
                        if label not in handled:
                            send("\r", label)
                            handled.add(label)
                        break
                if handled_dialog:
                    continue
                assert "Select login method:" not in current_screen, (
                    f"Configured ug launched {agent}'s account-login flow instead of its "
                    "gateway session:\n" + current_screen
                )
                if agent == "codex":
                    assert not (
                        "Sign in with ChatGPT" in current_screen
                        and "Provide your own API key" in current_screen
                    ), "Configured ug launched Codex's account-login flow:\n" + current_screen
                title = "Claude Code" if agent == "claude" else "Codex"
                title_ready = title in current_screen and "loading" not in current_screen.lower()
                if title_ready and re.search(r"(?m)^\s*[❯›>]\s*(?!\d+[.)])", current_screen):
                    ready_since = ready_since or time.monotonic()
                    if time.monotonic() - ready_since >= 1:
                        actions.append({"reason": "prompt-ready", "screen": current_screen})
                        return
                else:
                    ready_since = None
                time.sleep(0.05)
            timed_out = True
            raise AssertionError(f"TUI did not reach a usable prompt within 120s:\n{visible()}")

        def wait_for_completion() -> None:
            nonlocal timed_out
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                read()
                if ended:
                    raise AssertionError(f"TUI exited before the task completed:\n{visible()}")
                if completion(self):
                    return
                time.sleep(0.05)
            timed_out = True
            raise AssertionError(f"TUI did not complete within {timeout}s:\n{visible()}")

        failure: BaseException | None = None
        returncode: int | None = None
        try:
            wait_for_prompt()
            send(prompt, "type task prompt")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                read()
                if prompt.split()[-1] in visible():
                    break
                assert not ended, f"TUI exited while rendering the task prompt:\n{visible()}"
                time.sleep(0.05)
            else:
                timed_out = True
                raise AssertionError(f"TUI did not render the task prompt:\n{visible()}")
            send("\r", "submit task prompt")
            wait_for_completion()
            send("/exit", "type exit command")
            send("\r", "submit exit command")
            deadline = time.monotonic() + 30
            while not ended and time.monotonic() < deadline:
                read()
                time.sleep(0.05)
            if not ended:
                timed_out = True
                raise AssertionError(f"TUI did not exit within 30s:\n{visible()}")
            child.close(force=False)
            returncode = child.exitstatus if child.exitstatus is not None else 1
            if returncode != 0:
                raise AssertionError(
                    f"Interactive command failed ({returncode}): {command}\n{visible()}"
                )
            return CommandResult(
                command,
                returncode,
                self._redact("".join(output)),
                "",
            )
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if not ended:
                with contextlib.suppress(OSError):
                    os.killpg(child.pid, signal.SIGTERM)
                with contextlib.suppress(OSError):
                    os.killpg(child.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                child.close(force=True)
            self._record_tui_artifact(
                command,
                stdout="".join(output),
                returncode=returncode if returncode is not None else child.exitstatus,
                timed_out=timed_out,
                actions=actions,
                screen=visible(),
                failure=failure,
            )

    def read_json(self, relative_path: str) -> object:
        return json.loads(self.home.joinpath(relative_path).read_text(encoding="utf-8"))

    def transcripts(self, agent: str) -> dict[str, list[dict]]:
        root = self.home / (".claude/projects" if agent == "claude" else ".codex/sessions")
        result: dict[str, list[dict]] = {}
        paths = sorted(root.rglob("*.jsonl")) if root.is_dir() else []
        for path in paths:
            records: list[dict] = []
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AssertionError(
                        f"Invalid {agent} transcript {path}:{line_number}"
                    ) from exc
                if isinstance(row, dict):
                    records.append(row)
            result[str(path.relative_to(root))] = records
        return result

    def transcript_contains(self, agent: str, *needles: str) -> bool:
        """Return true when one complete transcript record contains every needle."""
        root = self.home / (".claude/projects" if agent == "claude" else ".codex/sessions")
        if not root.is_dir():
            return False
        for path in root.rglob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    record = json.loads(line)
                    if isinstance(record, dict):
                        text = "\n".join(_json_strings(record))
                        if all(needle in text for needle in needles):
                            return True
        return False

    def close(self) -> None:
        self._temporary.cleanup()


def _jsonl_records(transcripts: dict[str, list[dict]]) -> list[dict]:
    return [record for rows in transcripts.values() for record in rows]


def _tool_events(transcripts: dict[str, list[dict]]) -> tuple[dict[str, dict], dict[str, dict]]:
    calls: dict[str, dict] = {}
    results: dict[str, dict] = {}
    for record in _jsonl_records(transcripts):
        for node in _dict_nodes(record):
            event_type = str(node.get("type", "")).lower()
            if event_type in _CALL_TYPES:
                identifier = _call_id(node)
                if identifier:
                    calls[identifier] = node
            elif event_type in _RESULT_TYPES:
                identifier = _call_id(node)
                if identifier:
                    results[identifier] = node
    return calls, results


def correlated_tool_evidence(
    transcripts: dict[str, list[dict]], tool_fragment: str, marker: str
) -> dict[str, object]:
    calls, results = _tool_events(transcripts)
    for identifier, call in calls.items():
        call_text = "\n".join(_json_strings(call))
        result = results.get(identifier)
        result_text = "\n".join(_json_strings(result)) if result else ""
        if tool_fragment in call_text and marker in call_text and marker in result_text:
            return {"call_id": identifier, "call": call, "result": result}
    raise AssertionError(
        f"No correlated {tool_fragment!r} call/result carried marker {marker!r}; "
        f"calls={list(calls)} results={list(results)}"
    )


def structured_final_answers(
    agent: str, command: CommandResult, transcripts: dict[str, list[dict]]
) -> list[str]:
    answers: list[str] = []
    for line in command.stdout.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            if (
                agent == "claude"
                and row.get("type") == "result"
                and isinstance(row.get("result"), str)
            ):
                answers.append(row["result"])
            if agent == "codex" and row.get("type") == "item.completed":
                item = row.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        answers.append(text)
    for record in _jsonl_records(transcripts):
        if agent == "claude" and record.get("type") == "assistant":
            message = record.get("message")
            if isinstance(message, dict):
                answers.extend(
                    text
                    for part in message.get("content", [])
                    if isinstance(part, dict)
                    for text in [part.get("text")]
                    if isinstance(text, str)
                )
        if agent == "codex" and record.get("type") == "event_msg":
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "task_complete":
                text = payload.get("last_agent_message")
                if isinstance(text, str):
                    answers.append(text)
    return answers


class WorkspaceApi:
    """Small JSON/raw adapter over a ``WorkspaceClient``'s public API client."""

    def __init__(self, workspace_client):
        self.client = workspace_client

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        return self.client.api_client.do(
            method,
            path=path,
            query=query,
            body=body,
            headers={"Accept": "application/json", **(headers or {})},
        )

    def json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict | list:
        response = self.request(
            method,
            query=query,
            body=body,
            path=path,
            headers=headers,
        )
        if not isinstance(response, (dict, list)):
            raise AssertionError(f"Expected JSON from {method} {path}, got {type(response)!r}")
        return response

    def raw_bytes(self, path: str) -> bytes:
        response = self.client.api_client.do("GET", path=path, raw=True)
        stream = response.get("contents") if isinstance(response, dict) else response
        if not hasattr(stream, "read"):
            raise AssertionError(f"Expected streamed bytes from {path}, got {type(response)!r}")
        try:
            value = stream.read()
        finally:
            stream.close()
        if not isinstance(value, bytes):
            raise AssertionError(f"Expected bytes from {path}, got {type(value)!r}")
        return value


def _sse_json(data: bytes) -> list[dict]:
    text = data.decode("utf-8", errors="strict")
    stripped = text.strip()
    if stripped:
        with contextlib.suppress(json.JSONDecodeError):
            value = json.loads(stripped)
            if isinstance(value, dict):
                return [value]
    events: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        value = json.loads(payload)
        if isinstance(value, dict):
            events.append(value)
    return events


class McpRpc:
    def __init__(self, api: WorkspaceApi, path: str):
        self.api = api
        self.path = path
        self.session_id: str | None = None
        self._next_id = 1

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = self.api.client.api_client.do(
            "POST",
            path=self.path,
            headers=headers,
            body=body,
            raw=True,
            response_headers=["mcp-session-id"],
        )
        if isinstance(response, dict):
            self.session_id = response.get("mcp-session-id") or response.get("Mcp-Session-Id")
            stream = response.get("contents")
        else:
            stream = response
        if not hasattr(stream, "read"):
            raise AssertionError(f"MCP {method} did not return a response stream")
        try:
            events = _sse_json(stream.read())
        finally:
            stream.close()
        matches = [event for event in events if event.get("id") == request_id]
        if not matches:
            raise AssertionError(f"MCP {method} returned no JSON-RPC response: {events!r}")
        result = matches[-1]
        if "error" in result:
            raise AssertionError(f"MCP {method} failed: {result['error']!r}")
        return result

    def notify(self, method: str, params: dict | None = None) -> None:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = self.api.client.api_client.do(
            "POST",
            path=self.path,
            headers=headers,
            body={"jsonrpc": "2.0", "method": method, "params": params or {}},
        )
        # Streamable-HTTP servers commonly acknowledge notifications with HTTP 202 and no body;
        # api_client.do returns None for that successful response and raises on HTTP failures.
        if response is not None and not isinstance(response, (dict, list)):
            raise AssertionError(f"MCP notification returned {type(response)!r}")


def mcp_tools(result: dict) -> list[dict]:
    payload = result.get("result")
    if not isinstance(payload, dict):
        raise AssertionError(f"MCP response had no result object: {result!r}")
    tools = payload.get("tools")
    if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
        raise AssertionError(f"MCP tools/list returned an invalid tool list: {result!r}")
    return tools


def listed_mcp_names(output: str, dotted_name: str) -> bool:
    dashed = dotted_name.replace(".", "-")
    return any(dotted_name in line or dashed in line for line in output.splitlines())
