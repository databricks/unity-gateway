"""Offline answer-delivery checks; no agent inference or gateway requests."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
from typing import Any

import pytest

from ucode import claude_answer_delivery as output
from ucode.claude_answer_delivery import parse_selection, resolve_answer

_SESSION = "parent-session"
_ANSWERS = {"child-a": "complete answer A", "child-b": "complete answer B"}


def _transcript(
    final_text: str,
    *,
    child_ids: tuple[str, ...] = ("child-a",),
    background_children: frozenset[str] = frozenset(),
    async_children: frozenset[str] = frozenset(),
    tool_name: str = "Agent",
) -> list[dict[str, Any]]:
    tool_ids = {child_id: f"tool-{child_id}" for child_id in child_ids}
    calls = []
    for child_id, tool_id in tool_ids.items():
        tool_input = {"prompt": child_id}
        if child_id in background_children:
            tool_input["run_in_background"] = True
        calls.append({"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_input})

    events: list[dict[str, Any]] = [
        {
            "type": "assistant",
            "session_id": _SESSION,
            "parent_tool_use_id": None,
            "message": {"content": calls},
        }
    ]
    for child_id, tool_id in tool_ids.items():
        events.append(
            {
                "type": "system",
                "subtype": "task_started",
                "session_id": _SESSION,
                "task_id": child_id,
                "tool_use_id": tool_id,
                "spawn_depth": 1,
                "task_type": "local_agent",
            }
        )
        if child_id in async_children:
            events.append(
                {
                    "type": "user",
                    "session_id": _SESSION,
                    "parent_tool_use_id": None,
                    "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]},
                    "tool_use_result": {
                        "status": "async_launched",
                        "isAsync": True,
                        "agentId": child_id,
                    },
                }
            )
        if child_id in background_children or child_id in async_children:
            events.extend(
                [
                    {
                        "type": "assistant",
                        "session_id": _SESSION,
                        "parent_tool_use_id": tool_id,
                        "message": {"content": [{"type": "text", "text": _ANSWERS[child_id]}]},
                    },
                    {
                        "type": "system",
                        "subtype": "task_notification",
                        "session_id": _SESSION,
                        "task_id": child_id,
                        "tool_use_id": tool_id,
                        "status": "completed",
                        "summary": _ANSWERS[child_id],
                    },
                ]
            )
        else:
            events.extend(
                [
                    {
                        "type": "system",
                        "subtype": "task_notification",
                        "session_id": _SESSION,
                        "task_id": child_id,
                        "tool_use_id": tool_id,
                        "status": "completed",
                        "summary": "notice",
                    },
                    {
                        "type": "user",
                        "session_id": _SESSION,
                        "parent_tool_use_id": None,
                        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]},
                        "tool_use_result": {
                            "status": "completed",
                            "agentId": child_id,
                            "content": [{"type": "text", "text": _ANSWERS[child_id]}],
                        },
                    },
                ]
            )
    events.extend(
        [
            {
                "type": "assistant",
                "session_id": _SESSION,
                "parent_tool_use_id": None,
                "message": {"content": [{"type": "text", "text": final_text}]},
            },
            {"type": "result", "session_id": _SESSION, "result": final_text},
        ]
    )
    return events


def _stdout(events: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def test_parse_selection_accepts_child_and_ordered_composition() -> None:
    assert parse_selection('{"use_agent_answer":"child-a"}') == ("child-a", None)
    composition = '{"answer_parts":["start ",{"use_agent_answer":"child-a"}]}'
    assert parse_selection(composition) == (json.loads(composition), None)
    assert parse_selection('{"answer":"ordinary answer"}') == (None, None)


@pytest.mark.parametrize(
    ("final_text", "reason"),
    [
        ('{"answer_parts":[],"answer_parts":[]}', "duplicate JSON keys"),
        ('{"answer_parts":[{"use_agent_answer":"child-a","extra":1}]}', "entries"),
        ('{"answer_parts":["literal"],"extra":1}', "exactly one key"),
        ('{"answer_parts":', "malformed"),
        ('{"answer_parts":"not-a-list"}', "list"),
        (
            '{"answer_parts":[{"use_agent_answer":"child-a"}]}\n{"use_agent_answer":"child-a"}',
            "ambiguous",
        ),
        ('{"use\\u005fagent_answer":"a","use\\u005fagent_answer":"b"}', "duplicate JSON keys"),
    ],
)
def test_parse_selection_rejects_invalid_reserved_json(final_text: str, reason: str) -> None:
    selection, error = parse_selection(final_text)
    assert selection is None
    assert error is not None and reason in error


@pytest.mark.parametrize("tool_name", ["Agent", "Task"])
def test_resolve_answer_accepts_owned_foreground_result_and_sha_receipt(tool_name: str) -> None:
    final_text = '{"use_agent_answer":"child-a"}'
    answer, receipt, error = resolve_answer(
        final_text, _stdout(_transcript(final_text, tool_name=tool_name)), _SESSION
    )

    assert answer == _ANSWERS["child-a"]
    assert error is None
    assert receipt is not None
    assert receipt["source"] == "foreground_tool_result"
    assert receipt["source_tool"] == tool_name
    assert receipt["source_agent_id"] == "child-a"
    assert receipt["source_session_id"] == _SESSION
    assert receipt["answer_sha256"] == hashlib.sha256(answer.encode("utf-8")).hexdigest()


def test_resolve_answer_composes_ordered_literals_and_child_receipts() -> None:
    final_text = json.dumps(
        {
            "answer_parts": [
                "A: ",
                {"use_agent_answer": "child-a"},
                "; B: ",
                {"use_agent_answer": "child-b"},
            ]
        }
    )
    answer, receipt, error = resolve_answer(
        final_text,
        _stdout(
            _transcript(
                final_text,
                child_ids=("child-a", "child-b"),
                background_children=frozenset({"child-b"}),
            )
        ),
        _SESSION,
    )

    assert answer == "A: complete answer A; B: complete answer B"
    assert error is None
    assert receipt is not None
    assert receipt["source"] == "answer_composition"
    assert receipt["source_agent_ids"] == ["child-a", "child-b"]
    assert receipt["answer_sha256"] == hashlib.sha256(answer.encode("utf-8")).hexdigest()
    components = receipt["components"]
    assert isinstance(components, list)
    assert components[0] == {
        "type": "literal",
        "text_sha256": hashlib.sha256(b"A: ").hexdigest(),
    }
    assert components[3] == {
        "type": "child_answer",
        "source_agent_id": "child-b",
        "source": "background_task_notification",
        "answer_sha256": hashlib.sha256(_ANSWERS["child-b"].encode("utf-8")).hexdigest(),
    }


def test_async_child_is_source_linked_without_background_input_and_notification_must_match() -> (
    None
):
    final_text = '{"use_agent_answer":"child-a"}'
    events = _transcript(final_text, async_children=frozenset({"child-a"}))
    root_call = events[0]["message"]["content"][0]
    assert "run_in_background" not in root_call["input"]

    answer, receipt, error = resolve_answer(final_text, _stdout(events), _SESSION)
    assert (answer, error) == (_ANSWERS["child-a"], None)
    assert receipt is not None and receipt["source"] == "background_task_notification"

    notification = next(event for event in events if event.get("subtype") == "task_notification")
    notification["summary"] = "stale child text"
    answer, receipt, error = resolve_answer(final_text, _stdout(events), _SESSION)
    assert answer is None and receipt is not None and receipt["status"] == "rejected"
    assert error is not None and "complete child final text" in error


def test_resume_invalidates_old_answer_and_only_accepts_latest_child_revision() -> None:
    final_text = '{"use_agent_answer":"child-a"}'
    resume = [
        {
            "type": "assistant",
            "session_id": _SESSION,
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-resume",
                        "name": "SendMessage",
                        "input": {"to": "child-a", "message": "revise"},
                    }
                ]
            },
        },
        {
            "type": "system",
            "subtype": "task_started",
            "session_id": _SESSION,
            "task_id": "child-a",
            "tool_use_id": "tool-resume",
            "spawn_depth": 1,
            "task_type": "local_agent",
            "parent_tool_use_id": "tool-child-a",
        },
    ]
    events = _transcript(final_text)
    events[-2:-2] = resume
    assert resolve_answer(final_text, _stdout(events), _SESSION)[2] == (
        "selected child resume has no completed answer"
    )

    latest_answer = "revised complete answer"
    events[-2:-2] = [
        {
            "type": "assistant",
            "session_id": _SESSION,
            "parent_tool_use_id": "tool-child-a",
            "message": {"content": [{"type": "text", "text": latest_answer}]},
        },
        {
            "type": "system",
            "subtype": "task_notification",
            "session_id": _SESSION,
            "task_id": "child-a",
            "tool_use_id": "tool-resume",
            "status": "completed",
            "summary": latest_answer,
        },
    ]
    answer, receipt, error = resolve_answer(final_text, _stdout(events), _SESSION)
    assert (answer, error) == (latest_answer, None)
    assert receipt is not None
    assert receipt["source"] == "background_task_notification"
    assert receipt["answer_sha256"] == hashlib.sha256(latest_answer.encode("utf-8")).hexdigest()


def test_resolve_answer_rejects_wrong_parent_session_and_nested_child() -> None:
    final_text = '{"use_agent_answer":"child-a"}'
    events = _transcript(final_text)
    events[1]["spawn_depth"] = 2
    assert "owned direct local_agent" in resolve_answer(final_text, _stdout(events), _SESSION)[2]

    events = _transcript(final_text)
    events[-1]["session_id"] = "other-session"
    assert "terminal result" in resolve_answer(final_text, _stdout(events), _SESSION)[2]


def _delivered_stdout(final='{"use_agent_answer":"child-a"}'):
    events = _transcript(final)
    events[-1].update(usage={"output_tokens": 12}, total_cost_usd=0.25)
    return "\n".join(json.dumps(event) for event in events)


@pytest.mark.parametrize("mode", ["text", "json", "stream-json"])
def test_output_arguments_preserve_caller_options_and_separator(mode):
    args = [
        "/claude",
        "-p",
        "--model",
        "caller-model",
        "--output-format=" + mode,
        "--permission-mode",
        "default",
        "--",
        "--output-format=not-an-option",
    ]
    rewritten, requested, verbose = output.stream_arguments(args)
    assert requested == mode and not verbose
    assert rewritten == [
        "/claude",
        "-p",
        "--model",
        "caller-model",
        "--permission-mode",
        "default",
        "--output-format",
        "stream-json",
        "--verbose",
        "--",
        "--output-format=not-an-option",
    ]


def test_resolved_output_keeps_cost_and_usage_and_replaces_complete_root_text():
    events, receipts, failed = output.deliver_events(_delivered_stdout())
    assert not failed
    assert events[-1]["result"] == "complete answer A"
    assert events[-2]["message"]["content"] == [{"type": "text", "text": "complete answer A"}]
    assert events[-1]["usage"] == {"output_tokens": 12}
    assert events[-1]["total_cost_usd"] == 0.25
    assert events[-1]["ug_answer_delivery"] == receipts[0]


def test_foreign_reference_is_a_delivery_error_without_a_selected_answer():
    events, receipts, failed = output.deliver_events(
        _delivered_stdout('{"use_agent_answer":"foreign"}')
    )
    assert failed and receipts[0]["status"] == "rejected"
    assert events[-1]["is_error"] and events[-1]["result"] == ""
    assert events[-2]["message"]["content"][0]["text"].startswith("Answer delivery rejected:")


@pytest.mark.parametrize("mode", ["text", "json", "stream-json"])
def test_real_subprocess_capture_renders_format_and_preserves_stdin_stderr_and_status(
    tmp_path, capfd, mode
):
    session = tmp_path / "env.json"
    source = tmp_path / "source.jsonl"
    source.write_text(_delivered_stdout())
    # A Python fixture supplies recorded events; the real output adapter owns process I/O.
    script = (
        "import sys; from pathlib import Path; "
        "assert sys.stdin.buffer.read() == b'caller input'; "
        f"assert Path({str(tmp_path / output.CAPABILITY_FILE)!r}).is_file(); "
        f"sys.stdout.write(Path({str(source)!r}).read_text()); "
        "sys.stderr.write('child diagnostic\\n')"
    )
    rc = output.run(
        [sys.executable, "-c", script, "--output-format", mode],
        session_path=session,
        stdin_prompt=b"caller input",
    )
    captured = capfd.readouterr()
    assert rc == 0 and captured.err == "child diagnostic\n"
    if mode == "text":
        assert captured.out == "complete answer A\n"
    elif mode == "json":
        assert json.loads(captured.out)["result"] == "complete answer A"
    else:
        assert json.loads(captured.out.splitlines()[-1])["result"] == "complete answer A"
    receipts = json.loads((tmp_path / "claude-answer-delivery-receipts.json").read_text())
    assert receipts[0]["status"] == "selected"
    assert not (tmp_path / output.CAPABILITY_FILE).exists()


def test_failed_subprocess_retains_nonzero_exit_and_cleans_capability(tmp_path, capfd):
    rc = output.run(
        [sys.executable, "-c", "raise SystemExit(7)"],
        session_path=tmp_path / "env.json",
        stdin_prompt=None,
    )
    assert rc == 7
    assert "no terminal result" in capfd.readouterr().err
    assert not (tmp_path / output.CAPABILITY_FILE).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX subprocess signal return codes")
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signaled_subprocess_returns_conventional_shell_status(tmp_path, capfd, sig):
    script = (
        "import os, signal; "
        f"signal.signal({int(sig)}, signal.SIG_DFL); os.kill(os.getpid(), {int(sig)})"
    )
    rc = output.run(
        [sys.executable, "-c", script], session_path=tmp_path / "env.json", stdin_prompt=None
    )
    assert rc == 128 + int(sig)
    assert not (tmp_path / output.CAPABILITY_FILE).exists()


def test_ordinary_answer_and_native_error_keep_their_values():
    events, receipts, failed = output.deliver_events(_delivered_stdout("ordinary complete answer"))
    assert events[-1]["result"] == "ordinary complete answer" and receipts == [] and not failed
    native_error = {"type": "result", "is_error": True, "errors": ["API error"], "result": ""}
    events, receipts, _ = output.deliver_events(json.dumps(native_error))
    assert events == [native_error] and receipts == []


def test_malformed_or_missing_terminal_stream_is_rejected():
    for source in ("broken", "[]", '{"type":"assistant"}'):
        with pytest.raises(ValueError):
            output.deliver_events(source)


def test_intermediate_selection_cannot_override_the_terminal_parent_answer():
    early = _delivered_stdout('{"use_agent_answer":"foreign"}')
    terminal = [
        {
            "type": "assistant",
            "session_id": "parent-session",
            "message": {"content": [{"type": "text", "text": "the final parent answer"}]},
        },
        {"type": "result", "session_id": "parent-session", "result": "the final parent answer"},
    ]
    events, receipts, failed = output.deliver_events(
        early + "\n" + "\n".join(json.dumps(event) for event in terminal)
    )
    assert not failed and receipts == []
    assert events[-1]["result"] == "the final parent answer"


def test_mismatched_terminal_result_cannot_hide_root_selection():
    events = [json.loads(line) for line in _delivered_stdout().splitlines()]
    events[-1]["result"] = "older parent text"
    delivered, receipts, failed = output.deliver_events(
        "\n".join(json.dumps(event) for event in events)
    )
    assert failed and receipts[0]["status"] == "rejected"
    assert delivered[-1]["result"] == ""
