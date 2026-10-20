"""Deliver accepted child answers at Claude's headless subprocess boundary."""

from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any

from ucode.config_io import atomic_write_json
from ucode.os_compatibility import subprocess_cross_os

CAPABILITY_FILE = "claude-answer-delivery"
AVAILABLE_CONTEXT = (
    "This UG print session supports explicit answer delivery. After reviewing the full "
    "caller requirements and completed child answers, end with exactly "
    '{"use_agent_answer":"<actual agentId>"}, or with exactly '
    '{"answer_parts":["literal text",{"use_agent_answer":"<actual agentId>"}]} to join '
    "literal text and complete child answers in order. Select only completed owned children. "
    "Otherwise answer normally."
)
UNAVAILABLE_CONTEXT = (
    "Explicit child-answer selection is unavailable on this output surface. Deliver a normal "
    "complete parent-written answer; do not return use_agent_answer or answer_parts directives."
)

_NATIVE_DELEGATION_TOOLS = frozenset({"Agent", "Task"})


def parse_selection(final_text: str) -> tuple[object | None, str | None]:
    """Read an explicit terminal answer selection after any parent-authored review."""
    duplicate_keys: list[str] = []

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicate_keys.append(key)
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    prefix = ""
    try:
        selection = json.loads(final_text, object_pairs_hook=unique_object)
    except (TypeError, json.JSONDecodeError, ValueError):
        duplicate_reserved = set(duplicate_keys) & {"use_agent_answer", "answer_parts"}
        if duplicate_reserved:
            return None, "answer selection contains duplicate JSON keys"
        prefix, _, terminal_line = final_text.rstrip().rpartition("\n")
        try:
            selection = json.loads(terminal_line, object_pairs_hook=unique_object)
        except (TypeError, json.JSONDecodeError, ValueError):
            duplicate_reserved = set(duplicate_keys) & {"use_agent_answer", "answer_parts"}
            if duplicate_reserved:
                return None, "answer selection contains duplicate JSON keys"
            if any(
                text.lstrip().startswith("{")
                and any(f'"{key}"' in text for key in ("use_agent_answer", "answer_parts"))
                for text in (terminal_line, final_text)
            ):
                return None, "malformed parent answer selection JSON"
            return None, None
    if not isinstance(selection, dict):
        return None, None

    reserved = {"use_agent_answer", "answer_parts"} & set(selection)
    if not reserved:
        return None, None
    for line in prefix.splitlines():
        try:
            preceding = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(preceding, dict) and ({"use_agent_answer", "answer_parts"} & set(preceding)):
            return None, "parent answer selection is ambiguous across multiple blocks"

    if reserved == {"use_agent_answer"}:
        if set(selection) != {"use_agent_answer"}:
            return None, "use_agent_answer selection must contain exactly one key"
        agent_id = selection["use_agent_answer"]
        if not isinstance(agent_id, str) or not agent_id or agent_id.strip() != agent_id:
            return None, "use_agent_answer must name a nonempty exact agentId"
        return agent_id, None

    if reserved != {"answer_parts"} or set(selection) != {"answer_parts"}:
        return None, "answer_parts selection must contain exactly one key"
    parts = selection["answer_parts"]
    if not isinstance(parts, list):
        return None, "answer_parts must be a list"
    child_ids: list[str] = []
    for part in parts:
        if isinstance(part, str):
            continue
        if not isinstance(part, dict) or set(part) != {"use_agent_answer"}:
            return (
                None,
                "answer_parts entries must be literal text or exact use_agent_answer references",
            )
        agent_id = part["use_agent_answer"]
        if not isinstance(agent_id, str) or not agent_id or agent_id.strip() != agent_id:
            return None, "use_agent_answer must name a nonempty exact agentId"
        child_ids.append(agent_id)
    if not child_ids:
        return None, "answer_parts must contain at least one child reference"
    return selection, None


def _content(event: dict) -> list[dict]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return (
        [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    )


def _text(event: dict) -> str | None:
    blocks = [block for block in _content(event) if block.get("type") == "text"]
    if not blocks or any(not isinstance(block.get("text"), str) for block in blocks):
        return None
    return "".join(block["text"] for block in blocks)


def _is_root(event: dict, session_id: str) -> bool:
    return (
        event.get("type") == "assistant"
        and event.get("session_id") == session_id
        and event.get("parent_tool_use_id") is None
    )


def _resolve_child(
    agent_id: str, events: list[dict], calls: dict[str, dict], session_id: str
) -> tuple[str, dict]:
    def owned_start(event: dict, tool_id: str) -> bool:
        return (
            event.get("session_id") == session_id
            and type(event.get("spawn_depth")) is int
            and event["spawn_depth"] == 1
            and event.get("task_type") == "local_agent"
            and event.get("owned_by_subagent") is not True
            and event.get("parent_tool_use_id") in (None, tool_id)
        )

    starts = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("type") == "system"
        and event.get("subtype") == "task_started"
        and event.get("task_id") == agent_id
        and isinstance(event.get("tool_use_id"), str)
        and event["tool_use_id"] in calls
    ]
    if len(starts) != 1:
        raise ValueError("selected agent is not a uniquely owned direct child")
    start_index, start = starts[0]
    root_tool_id = start["tool_use_id"]
    if not owned_start(start, root_tool_id):
        raise ValueError("selected agent is not an owned direct local_agent")
    tool_input = calls[root_tool_id].get("input")
    background = isinstance(tool_input, dict) and tool_input.get("run_in_background") is True
    tool_id = root_tool_id
    pending_resume = None
    resumed, started, failed = False, True, False
    answer = child_text = None
    source = ""

    for event in events[start_index + 1 :]:
        if _is_root(event, session_id):
            for block in _content(event):
                if (
                    block.get("type") == "tool_use"
                    and block.get("name") == "SendMessage"
                    and isinstance(block.get("id"), str)
                    and isinstance(block.get("input"), dict)
                    and block["input"].get("to") == agent_id
                ):
                    pending_resume = block["id"]
                    answer = child_text = None
                    resumed, started, background = True, False, True
        if event.get("type") == "system" and event.get("task_id") == agent_id:
            if event.get("subtype") == "task_started":
                if (
                    pending_resume is None
                    or event.get("tool_use_id") != pending_resume
                    or not owned_start(event, root_tool_id)
                ):
                    raise ValueError("selected child resume is not an owned root SendMessage")
                tool_id, pending_resume = pending_resume, None
                answer = child_text = None
                started, failed = True, False
            elif (
                event.get("subtype") == "task_notification"
                and event.get("session_id") == session_id
                and event.get("tool_use_id") == tool_id
            ):
                if event.get("status") != "completed":
                    answer, failed = None, True
                elif background and not failed:
                    summary = event.get("summary")
                    if (
                        not started
                        or not isinstance(summary, str)
                        or not summary
                        or summary != child_text
                    ):
                        raise ValueError(
                            "task_notification summary does not match the complete child final text"
                        )
                    answer, source = summary, "background_task_notification"
        if event.get("type") == "user":
            result = event.get("tool_use_result")
            if not isinstance(result, dict) or result.get("agentId") != agent_id:
                continue
            linked = event.get("session_id") == session_id and (
                event.get("parent_tool_use_id") == root_tool_id
                or result.get("tool_use_id") == root_tool_id
                or any(
                    block.get("type") == "tool_result" and block.get("tool_use_id") == root_tool_id
                    for block in _content(event)
                )
            )
            if not linked:
                raise ValueError(
                    "selected foreground result is not linked to its root Agent/Task call"
                )
            if result.get("isAsync") is True and result.get("status") == "async_launched":
                background = True
                continue
            if result.get("status") != "completed":
                answer, failed = None, True
            # Native Claude can recover partial text with status=completed after a failed task.
            if failed:
                continue
            content = result.get("content")
            if (
                resumed
                or not started
                or not isinstance(content, list)
                or not content
                or any(
                    not isinstance(block, dict)
                    or block.get("type") != "text"
                    or not isinstance(block.get("text"), str)
                    for block in content
                )
            ):
                raise ValueError("selected foreground result is stale, empty, or non-text")
            answer = "".join(block["text"] for block in content)
            if not answer:
                raise ValueError("selected foreground result is empty")
            source = "foreground_tool_result"
        if (
            event.get("type") == "assistant"
            and event.get("session_id") == session_id
            and event.get("parent_tool_use_id") == root_tool_id
            and background
            and started
        ):
            if (text := _text(event)) is not None:
                child_text, answer = text, None
    if failed or not answer:
        raise ValueError("selected child has no successfully completed answer")
    return answer, {
        "status": "selected",
        "source": source,
        "source_agent_id": agent_id,
        "source_tool": calls[root_tool_id]["name"],
        "source_tool_use_id": root_tool_id,
        "source_session_id": session_id,
        "answer_sha256": _sha256(answer),
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_selection(selection: Any, events: list[dict], session_id: str) -> tuple[str, dict]:
    calls = {
        block["id"]: block
        for event in events
        if _is_root(event, session_id)
        for block in _content(event)
        if block.get("type") == "tool_use"
        and isinstance(block.get("name"), str)
        and block["name"] in _NATIVE_DELEGATION_TOOLS
        and isinstance(block.get("id"), str)
        and block["id"]
    }
    if isinstance(selection, str):
        return _resolve_child(selection, events, calls, session_id)
    parts = selection["answer_parts"]
    child_ids = [part["use_agent_answer"] for part in parts if isinstance(part, dict)]
    children = {
        agent_id: _resolve_child(agent_id, events, calls, session_id)
        for agent_id in dict.fromkeys(child_ids)
    }
    answer = "".join(
        part if isinstance(part, str) else children[part["use_agent_answer"]][0] for part in parts
    )
    return answer, {
        "status": "selected",
        "source": "answer_composition",
        "source_agent_ids": child_ids,
        "source_session_id": session_id,
        "answer_sha256": _sha256(answer),
        "components": [
            {"type": "literal", "text_sha256": _sha256(part)}
            if isinstance(part, str)
            else {
                "type": "child_answer",
                "source_agent_id": part["use_agent_answer"],
                "source": children[part["use_agent_answer"]][1]["source"],
                "answer_sha256": children[part["use_agent_answer"]][1]["answer_sha256"],
            }
            for part in parts
        ],
    }


def has_streaming_input(arguments: list[str]) -> bool:
    """Stream-json input needs native control responses before stdin reaches EOF."""
    input_format = "text"
    args = iter(arguments)
    for arg in args:
        if arg == "--":
            break
        if arg == "--input-format":
            input_format = next(args, "")
        elif arg.startswith("--input-format="):
            input_format = arg.partition("=")[2]
    return input_format == "stream-json"


def stream_arguments(arguments: list[str]) -> tuple[list[str], str, bool]:
    """Capture native events without changing tools, models, permissions or sampling."""
    output_format = "text"
    verbose = False
    rewritten: list[str] = []
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg == "--":
            break
        if arg == "--output-format":
            if index + 1 == len(arguments):
                raise ValueError("--output-format requires text, json, or stream-json")
            index += 1
            output_format = arguments[index]
        elif arg.startswith("--output-format="):
            output_format = arg.partition("=")[2]
        else:
            verbose = verbose or arg == "--verbose"
            rewritten.append(arg)
        index += 1
    if output_format not in {"text", "json", "stream-json"}:
        raise ValueError("--output-format requires text, json, or stream-json")
    rewritten.extend(["--output-format", "stream-json"])
    if not verbose:
        rewritten.append("--verbose")
    rewritten.extend(arguments[index:])
    return rewritten, output_format, verbose


def deliver_events(stdout: str) -> tuple[list[dict], list[dict], bool]:
    """Resolve the terminal answer from one parsed transcript, then rewrite its output."""
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    if any(not isinstance(event, dict) for event in events):
        raise ValueError("Claude stream contains a non-object event")
    result_index = next(
        (i for i in reversed(range(len(events))) if events[i].get("type") == "result"), None
    )
    if result_index is None:
        raise ValueError("Claude emitted no terminal result")
    terminal = events[result_index]
    if terminal.get("is_error"):
        return events, [], True
    final_text, session_id = terminal.get("result"), terminal.get("session_id")
    if not isinstance(final_text, str):
        raise ValueError("Claude terminal result is missing its answer text")
    root_index = next(
        (
            i
            for i in reversed(range(len(events)))
            if _is_root(events[i], session_id) and _text(events[i]) is not None
        ),
        None,
    )
    root_text = _text(events[root_index]) if root_index is not None else None
    try:
        selection, error = parse_selection(root_text if root_text is not None else final_text)
        if selection is None and error is None:
            selection, error = parse_selection(final_text)
        if error is not None:
            raise ValueError(error)
        if selection is None:
            return events, [], False
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("parent terminal result has no session_id")
        if root_index is None or root_index >= result_index or root_text != final_text:
            raise ValueError(
                "parent answer selection does not match the latest root text and terminal result"
            )
        answer, receipt = _resolve_selection(selection, events[:root_index], session_id)
    except ValueError as exc:
        error = str(exc)
        answer, receipt = None, {"status": "rejected", "reason": error}
        terminal.update(is_error=True, subtype="error_during_execution", result="", errors=[error])
    else:
        terminal["result"] = answer
    terminal["ug_answer_delivery"] = receipt
    if root_index is not None:
        events[root_index]["message"]["content"] = [
            block for block in _content(events[root_index]) if block.get("type") != "text"
        ] + [{"type": "text", "text": answer or f"Answer delivery rejected: {error}"}]
    return events, [receipt], error is not None


def run(argv: list[str], *, session_path: Path, stdin_prompt: bytes | None) -> int:
    """Buffer headless output, validate selection, then render the requested format."""
    buffered = not has_streaming_input(argv)
    output_format, verbose = "text", False
    if buffered:
        argv, output_format, verbose = stream_arguments(argv)
    marker = session_path.parent / CAPABILITY_FILE
    if buffered:
        marker.touch()
    try:
        with TemporaryFile() as captured:
            proc = subprocess_cross_os.popen(
                argv,
                stdin=subprocess.PIPE if stdin_prompt is not None else None,
                stdout=captured if buffered else None,
            )
            try:
                proc.communicate(input=stdin_prompt)
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGINT)
                proc.wait()
                return _exit_status(proc.returncode) or 130
            if not buffered:
                return _exit_status(proc.returncode)
            captured.seek(0)
            try:
                events, receipts, failed = deliver_events(captured.read().decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                print(f"Claude answer delivery failed: {exc}", file=sys.stderr)
                return _exit_status(proc.returncode) or 1
        atomic_write_json(session_path.parent / "claude-answer-delivery-receipts.json", receipts)
        results = [event for event in events if event.get("type") == "result"]
        if output_format == "stream-json":
            for event in events:
                print(json.dumps(event, ensure_ascii=False))
        elif output_format == "json":
            print(json.dumps(events if verbose else results[-1], ensure_ascii=False))
        else:
            result = results[-1]
            if result.get("is_error"):
                print(
                    "Claude answer delivery failed: " + str(result.get("errors", [])),
                    file=sys.stderr,
                )
            else:
                print(result.get("result", ""))
        return _exit_status(proc.returncode) or (1 if failed else 0)
    finally:
        if buffered:
            marker.unlink(missing_ok=True)


def _exit_status(returncode: int) -> int:
    # subprocess uses negative signal numbers; sys.exit needs a shell status.
    return 128 - returncode if returncode < 0 else returncode
