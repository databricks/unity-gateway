"""Deliver accepted child answers at Claude's headless subprocess boundary."""

from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, cast

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
_ParentContext = tuple[list[dict[str, Any]], int, dict[str, tuple[str, int, bool]]]


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


def _resolve_answer(
    final_text: str,
    stdout: str,
    result_session_id: object,
    *,
    _selected_agent_id: str | None = None,
    _parent_context: _ParentContext | None = None,
) -> tuple[str | None, dict[str, object] | None, str | None]:
    """Resolve a single-child selection or an ordered parent-authored composition."""

    def reject(reason: str) -> tuple[None, dict[str, object], str]:
        return None, {"status": "rejected", "reason": reason}, reason

    def event_text(event: dict[str, Any]) -> str | None:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return None
        blocks = [
            block for block in content if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not blocks or any(not isinstance(block.get("text"), str) for block in blocks):
            return None
        return "".join(block["text"] for block in blocks)

    composition_parts: list[Any] | None = None
    if _parent_context is None:
        selection, selection_error = parse_selection(final_text)
        if selection_error is not None:
            return reject(selection_error)
        if selection is None:
            return None, None, None
        if isinstance(selection, dict):
            composition_parts = cast(list[Any], selection["answer_parts"])
            child_ids: list[str] = [
                part["use_agent_answer"] for part in composition_parts if isinstance(part, dict)
            ]
            agent_id = child_ids[0]
        else:
            agent_id = cast(str, selection)
            child_ids = [agent_id]
        if not isinstance(result_session_id, str) or not result_session_id:
            return reject("parent terminal result has no session_id")

        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return reject("parent transcript contains malformed JSON")
            if not isinstance(event, dict):
                return reject("parent transcript contains a non-object event")
            events.append(event)

        root_text = None
        root_text_index = -1
        for index, event in enumerate(events):
            if (
                event.get("type") == "assistant"
                and event.get("parent_tool_use_id") is None
                and event.get("session_id") == result_session_id
            ):
                text = event_text(event)
                if text is not None:
                    root_text, root_text_index = text, index
        if root_text != final_text:
            return reject("parent answer selection is not the latest root assistant text")
        result_events = [
            event for event in events[root_text_index + 1 :] if event.get("type") == "result"
        ]
        if (
            not result_events
            or result_events[-1].get("session_id") != result_session_id
            or result_events[-1].get("result") != final_text
        ):
            return reject(
                "parent answer selection is not the parent terminal result for this session"
            )

        root_calls: dict[str, tuple[str, int, bool]] = {}
        for index, event in enumerate(events[: root_text_index + 1]):
            if (
                event.get("type") != "assistant"
                or event.get("parent_tool_use_id") is not None
                or event.get("session_id") != result_session_id
            ):
                continue
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, tool_id = block.get("name"), block.get("id")
                if (
                    isinstance(name, str)
                    and name in _NATIVE_DELEGATION_TOOLS
                    and isinstance(tool_id, str)
                    and tool_id
                ):
                    tool_input = block.get("input")
                    root_calls[tool_id] = (
                        name,
                        index,
                        isinstance(tool_input, dict)
                        and tool_input.get("run_in_background") is True,
                    )
        parent_context = (events, root_text_index, root_calls)

        if composition_parts is not None:
            answers: dict[str, str] = {}
            child_receipts: dict[str, dict[str, object]] = {}
            for child_id in dict.fromkeys(child_ids):
                child_answer, child_receipt, child_error = _resolve_answer(
                    final_text,
                    stdout,
                    result_session_id,
                    _selected_agent_id=child_id,
                    _parent_context=parent_context,
                )
                if child_error is not None or child_answer is None or child_receipt is None:
                    return reject(child_error or "selected child has no completed answer")
                answers[child_id] = child_answer
                child_receipts[child_id] = child_receipt
            composed = "".join(
                part if isinstance(part, str) else answers[part["use_agent_answer"]]
                for part in composition_parts
            )
            receipt = {
                "status": "selected",
                "source": "answer_composition",
                "source_agent_ids": child_ids,
                "source_session_id": result_session_id,
                "components": [
                    {
                        "type": "literal",
                        "text_sha256": hashlib.sha256(part.encode("utf-8")).hexdigest(),
                    }
                    if isinstance(part, str)
                    else {
                        "type": "child_answer",
                        "source_agent_id": part["use_agent_answer"],
                        "source": child_receipts[part["use_agent_answer"]]["source"],
                        "answer_sha256": child_receipts[part["use_agent_answer"]]["answer_sha256"],
                    }
                    for part in composition_parts
                ],
                "answer_sha256": hashlib.sha256(composed.encode("utf-8")).hexdigest(),
            }
            return composed, receipt, None
    else:
        if not isinstance(_selected_agent_id, str) or not _selected_agent_id:
            return reject("selected child id is invalid")
        agent_id = _selected_agent_id
        events, root_text_index, root_calls = _parent_context

    def owned_start(event: dict[str, Any], root_tool_id: str) -> bool:
        return (
            event.get("session_id") == result_session_id
            and type(event.get("spawn_depth")) is int
            and event.get("spawn_depth") == 1
            and event.get("task_type") == "local_agent"
            and event.get("owned_by_subagent") is not True
            and event.get("parent_tool_use_id") in (None, root_tool_id)
        )

    starts: list[tuple[int, str, Any]] = []
    for index, event in enumerate(events[: root_text_index + 1]):
        tool_id = event.get("tool_use_id")
        if (
            event.get("type") == "system"
            and event.get("subtype") == "task_started"
            and event.get("task_id") == agent_id
            and isinstance(tool_id, str)
            and tool_id in root_calls
        ):
            if not owned_start(event, tool_id):
                return reject("selected agent is not an owned direct local_agent")
            starts.append((index, tool_id, event.get("task_id")))
    if len(starts) != 1:
        return reject("selected agent is not a uniquely owned direct child")
    start_index, root_tool_id, active_task_id = starts[0]
    source_tool = root_calls[root_tool_id][0]
    answer: str | None = None
    source = ""
    current_resume = False
    current_background = root_calls[root_tool_id][2]
    current_started = True
    current_tool_use_id = root_tool_id
    pending_resume: str | None = None
    child_final_text: str | None = None
    failure = "selected child has no completed answer"

    for _index, event in enumerate(
        events[start_index + 1 : root_text_index + 1], start=start_index + 1
    ):
        if event.get("type") == "assistant" and event.get("parent_tool_use_id") is None:
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "SendMessage"
                        and isinstance(block.get("id"), str)
                        and isinstance(block.get("input"), dict)
                        and event.get("session_id") == result_session_id
                        and block["input"].get("to") == agent_id
                    ):
                        answer = None
                        source = ""
                        child_final_text = None
                        pending_resume = block["id"]
                        current_started = False
                        current_resume = True
                        current_background = True
                        failure = "selected child resume has no completed answer"
        if event.get("type") == "system" and event.get("subtype") == "task_started":
            if event.get("task_id") == agent_id:
                tool_id = event.get("tool_use_id")
                if (
                    tool_id != pending_resume
                    or not isinstance(tool_id, str)
                    or not owned_start(event, root_tool_id)
                ):
                    return reject("selected child resume is not an owned root SendMessage")
                answer = None
                source = ""
                child_final_text = None
                pending_resume = None
                current_started = True
                current_resume = True
                current_background = True
                current_tool_use_id = tool_id
                active_task_id = event.get("task_id")
            continue
        if event.get("type") == "user":
            result = event.get("tool_use_result")
            linked = (
                isinstance(result, dict)
                and event.get("session_id") == result_session_id
                and (
                    event.get("parent_tool_use_id") == root_tool_id
                    or result.get("tool_use_id") == root_tool_id
                    or (
                        isinstance(event.get("message"), dict)
                        and isinstance(event["message"].get("content"), list)
                        and any(
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and block.get("tool_use_id") == root_tool_id
                            for block in event["message"]["content"]
                        )
                    )
                )
            )
            if isinstance(result, dict) and result.get("agentId") == agent_id:
                if not linked:
                    return reject(
                        "selected foreground result is not linked to its root Agent/Task call"
                    )
                if result.get("isAsync") is True and result.get("status") == "async_launched":
                    current_background = True
                    continue
                if result.get("status") != "completed":
                    answer = None
                    failure = "selected child did not complete successfully"
                    continue
                content = result.get("content")
                if (
                    current_resume
                    or not current_started
                    or not isinstance(content, list)
                    or not content
                    or any(
                        not isinstance(block, dict)
                        or block.get("type") != "text"
                        or not isinstance(block.get("text"), str)
                        for block in content
                    )
                ):
                    return reject("selected foreground result is stale, empty, or non-text")
                answer = "".join(block["text"] for block in content)
                if not answer:
                    return reject("selected foreground result is empty")
                source = "foreground_tool_result"
                failure = ""
        if (
            event.get("type") == "assistant"
            and event.get("parent_tool_use_id") == root_tool_id
            and event.get("session_id") == result_session_id
            and current_background
            and current_started
        ):
            child_text = event_text(event)
            if child_text is not None:
                child_final_text = child_text
                answer = None
                failure = "background child has no matching completed task_notification"
        if event.get("type") == "system" and event.get("subtype") == "task_notification":
            linked_notification = (
                event.get("session_id") == result_session_id
                and event.get("tool_use_id") == current_tool_use_id
                and (
                    event.get("task_id") == agent_id
                    or (active_task_id is not None and event.get("task_id") == active_task_id)
                )
            )
            if linked_notification:
                if event.get("status") != "completed":
                    answer = None
                    failure = "selected child did not complete successfully"
                elif not current_background:
                    # Foreground completion is announced before its full tool_result arrives.
                    failure = "selected foreground child has no full tool result"
                else:
                    summary = event.get("summary")
                    if (
                        not current_started
                        or not isinstance(summary, str)
                        or not summary
                        or summary != child_final_text
                    ):
                        return reject(
                            "task_notification summary does not match the complete child final text"
                        )
                    answer = summary
                    source = "background_task_notification"
                    failure = ""

    if answer is None:
        return reject(failure)
    receipt = {
        "status": "selected",
        "source": source,
        "source_agent_id": agent_id,
        "source_tool": source_tool,
        "source_tool_use_id": root_tool_id,
        "source_session_id": result_session_id,
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
    }
    return answer, receipt, None


def resolve_answer(
    final_text: str, stdout: str, result_session_id: object
) -> tuple[str | None, dict[str, object] | None, str | None]:
    """Resolve the selected answer using the complete Claude stream-json transcript."""
    return _resolve_answer(final_text, stdout, result_session_id)


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
    """Resolve only the terminal parent answer, preserving earlier source events."""
    events: list[dict] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("Claude stream contains a non-object event")
        events.append(event)
    results = [event for event in events if event.get("type") == "result"]
    if not results:
        raise ValueError("Claude emitted no terminal result")
    terminal = results[-1]
    if terminal.get("is_error"):
        return events, [], True
    final_text = terminal.get("result")
    if not isinstance(final_text, str):
        raise ValueError("Claude terminal result is missing its answer text")
    root_message = None
    for message in reversed(events):
        if (
            message.get("type") != "assistant"
            or message.get("session_id") != terminal.get("session_id")
            or message.get("parent_tool_use_id") is not None
        ):
            continue
        body = message.get("message")
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, list):
            continue
        blocks = [
            block for block in content if isinstance(block, dict) and block.get("type") == "text"
        ]
        if blocks and all(isinstance(block.get("text"), str) for block in blocks):
            root_message = message
            root_text = "".join(block["text"] for block in blocks)
            selection, selection_error = parse_selection(root_text)
            if selection is not None or selection_error is not None:
                # Never allow an earlier result to hide a rejected terminal root selection.
                final_text = root_text
            break
    answer, receipt, error = resolve_answer(final_text, stdout, terminal.get("session_id"))
    receipts = [] if receipt is None else [receipt]
    if receipt is not None:
        terminal["ug_answer_delivery"] = receipt
    if error is not None:
        terminal.update(is_error=True, subtype="error_during_execution", result="", errors=[error])
    elif answer is not None:
        terminal["result"] = answer
    if root_message is not None and (answer is not None or error is not None):
        # Complete assistant rows and results agree; partial events remain source evidence.
        body = root_message["message"]
        body["content"] = [
            block
            for block in body["content"]
            if not isinstance(block, dict) or block.get("type") != "text"
        ] + [{"type": "text", "text": answer or f"Answer delivery rejected: {error}"}]
    return events, receipts, error is not None


def run(argv: list[str], *, session_path: Path, stdin_prompt: bytes | None) -> int:
    """Buffer headless output, validate selection, then render the requested format."""
    argv, output_format, verbose = stream_arguments(argv)
    marker = session_path.parent / CAPABILITY_FILE
    marker.touch()
    try:
        with TemporaryFile() as captured:
            proc = subprocess_cross_os.popen(
                argv,
                stdin=subprocess.PIPE if stdin_prompt is not None else None,
                stdout=captured,
            )
            try:
                proc.communicate(input=stdin_prompt)
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGINT)
                proc.wait()
                return _exit_status(proc.returncode) or 130
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
        marker.unlink(missing_ok=True)


def _exit_status(returncode: int) -> int:
    # subprocess uses negative signal numbers; sys.exit needs a shell status.
    return 128 - returncode if returncode < 0 else returncode
