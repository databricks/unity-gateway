"""Answer-delivery contracts using transcript inputs and real local subprocesses."""

from __future__ import annotations

import hashlib
import json
import sys

import pytest

from ucode import claude_answer_delivery as output
from ucode.ui import redirect_output_to_stderr

_SELECTION = '{"use_agent_answer":"a"}'


def _event(kind, **fields):
    return {"type": kind, "session_id": "parent", **fields}


def _message(kind, content, **fields):
    return _event(kind, message={"content": content}, **fields)


def _child(agent="a", *, background=False, resume=False, tool="Agent"):
    root_tool = f"tool-{agent}"
    tool_id = f"resume-{agent}" if resume else root_tool
    text = f"revised {agent}" if resume else f"answer {agent}"
    call = _message(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": tool_id,
                "name": "SendMessage" if resume else tool,
                "input": {"to": agent}
                if resume
                else {"prompt": agent, "run_in_background": background},
            }
        ],
    )
    start = _event(
        "system",
        subtype="task_started",
        task_id=agent,
        tool_use_id=tool_id,
        spawn_depth=1,
        task_type="local_agent",
        parent_tool_use_id=root_tool,
    )
    notice = _event(
        "system",
        subtype="task_notification",
        task_id=agent,
        tool_use_id=tool_id,
        status="completed",
        summary=text,
    )
    content = [{"type": "text", "text": text}]
    if background or resume:
        finish = [_message("assistant", content, parent_tool_use_id=root_tool), notice]
    else:
        finish = [
            notice,
            _message(
                "user",
                [{"type": "tool_result", "tool_use_id": root_tool}],
                tool_use_result={"status": "completed", "agentId": agent, "content": content},
            ),
        ]
    return [call, start, *finish]


def _transcript(children=None, final=_SELECTION):
    return [
        *(_child() if children is None else children),
        _message("assistant", [{"type": "text", "text": final}]),
        _event("result", result=final, usage={"output_tokens": 12}, total_cost_usd=0.25),
    ]


def _stdout(events):
    return "\n".join(json.dumps(event) for event in events)


def _deliver(events):
    return output.deliver_events(_stdout(events))


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_delivers_owned_answer_with_receipt_and_preserves_usage(tool):
    events, receipts, failed = _deliver(_transcript(_child(tool=tool)))
    assert not failed and events[-1]["result"] == "answer a"
    assert events[-2]["message"]["content"] == [{"type": "text", "text": "answer a"}]
    assert events[-1]["usage"] == {"output_tokens": 12}
    assert events[-1]["total_cost_usd"] == 0.25
    assert events[-1]["ug_answer_delivery"] == receipts[0]
    assert receipts[0]["source_tool"] == tool
    assert receipts[0]["answer_sha256"] == hashlib.sha256(b"answer a").hexdigest()
    assert "answer a" not in json.dumps(receipts)


def test_composes_literals_and_complete_foreground_and_background_answers():
    final = json.dumps(
        {"answer_parts": ["A: ", {"use_agent_answer": "a"}, "; B: ", {"use_agent_answer": "b"}]}
    )
    events, receipts, failed = _deliver(_transcript(_child() + _child("b", background=True), final))
    assert not failed and events[-1]["result"] == "A: answer a; B: answer b"
    assert receipts[0]["source_agent_ids"] == ["a", "b"]
    assert [part.get("source") for part in receipts[0]["components"]] == [
        None,
        "foreground_tool_result",
        None,
        "background_task_notification",
    ]


@pytest.mark.parametrize(
    ("index", "field", "value"),
    [
        (1, "spawn_depth", 2),
        (1, "session_id", "foreign"),
        (2, "status", "failed"),
        (2, "status", "stopped"),
        (3, "session_id", "foreign"),
        (-1, "result", "stale parent answer"),
    ],
)
def test_rejects_unowned_failed_and_stale_selections(index, field, value):
    source = _transcript()
    source[index][field] = value
    events, receipts, failed = _deliver(source)
    assert failed and receipts[0]["status"] == "rejected"
    assert events[-1]["is_error"] and events[-1]["result"] == ""
    assert events[-2]["message"]["content"][0]["text"].startswith("Answer delivery rejected:")


@pytest.mark.parametrize(
    "final",
    [
        '{"use_agent_answer":"foreign"}',
        '{"answer_parts":',
        'Review\n{"answer_parts":',
        _SELECTION + '\n{"answer_parts":',
        '{"use_agent_answer":"a","use_agent_answer":"b"}',
        '{"answer_parts":[{"use_agent_answer":"a","extra":1}]}',
        '{"answer_parts":["only literal text"]}',
        _SELECTION + "\n" + _SELECTION,
        '{\n  "use_agent_answer": "a"\n}\n' + _SELECTION,
        'Review\n{\n  "answer_parts": [{"use_agent_answer": "a"}]\n}\n' + _SELECTION,
    ],
)
def test_rejects_missing_children_and_invalid_selections(final):
    events, receipts, failed = _deliver(_transcript(final=final))
    assert failed and receipts[0]["status"] == "rejected"
    assert events[-1]["result"] == ""


def test_requires_unique_child_and_complete_matching_background_notification():
    duplicate = _child() + _child()
    assert _deliver(_transcript(duplicate))[2]
    children = _child(background=True)
    assert _deliver(_transcript(children[:-1]))[2]
    children[-1]["summary"] = "partial answer"
    assert _deliver(_transcript(children))[2]


@pytest.mark.parametrize("acknowledgment_index", [2, 4])
def test_recognizes_a_foreground_call_that_was_launched_asynchronously(acknowledgment_index):
    children = _child(background=True)
    children[0]["message"]["content"][0]["input"]["run_in_background"] = False
    children.insert(
        acknowledgment_index,
        _message(
            "user",
            [{"type": "tool_result", "tool_use_id": "tool-a"}],
            tool_use_result={"status": "async_launched", "isAsync": True, "agentId": "a"},
        ),
    )
    events, receipts, failed = _deliver(_transcript(children))
    assert not failed and events[-1]["result"] == "answer a"
    assert receipts[0]["source"] == "background_task_notification"


@pytest.mark.parametrize("invalid", ["foreign_session", "foreign_tool", "partial_summary"])
def test_late_background_acknowledgment_preserves_ownership_and_complete_answer_checks(invalid):
    children = _child(background=True)
    children[0]["message"]["content"][0]["input"].pop("run_in_background")
    acknowledgment = _message(
        "user",
        [{"type": "tool_result", "tool_use_id": "tool-a"}],
        tool_use_result={"status": "async_launched", "isAsync": True, "agentId": "a"},
    )
    if invalid == "foreign_session":
        acknowledgment["session_id"] = "foreign"
    elif invalid == "foreign_tool":
        acknowledgment["message"]["content"][0]["tool_use_id"] = "foreign"
    else:
        children[-1]["summary"] = "partial"
    assert _deliver(_transcript([*children, acknowledgment]))[2]


@pytest.mark.parametrize("initial_status", ["completed", "failed"])
def test_resume_invalidates_old_answer_until_new_revision_completes(initial_status):
    children = _child()
    children[2]["status"] = initial_status
    resume = _child(resume=True)
    for incomplete in (resume[:1], resume[:2], resume[:3]):
        assert _deliver(_transcript(children + incomplete))[2]
    events, _, failed = _deliver(_transcript(children + resume))
    assert not failed and events[-1]["result"] == "revised a"
    resume[-1]["status"] = "stopped"
    assert _deliver(_transcript(children + resume))[2]


def test_ordinary_final_answer_and_native_errors_are_preserved():
    source = _transcript(final="ordinary answer")
    events, receipts, failed = _deliver(_transcript() + source)
    assert not failed and receipts == [] and events[-1] == source[-1]
    error = _event("result", is_error=True, errors=["API error"], result="")
    assert _deliver([error]) == ([error], [], True)


@pytest.mark.parametrize(
    "final",
    [
        'A complete report.\n{"use_agent_answer":"foreign"} is an example only.',
        '{"answer_parts":[{"use_agent_answer":"foreign"}]} describes the protocol.',
        '{"use_agent_answer":"foreign"}\nThis is documentation, not a selection.',
        '{"use_agent_answer":"a"}\n{"use_agent_answer":"b"} are protocol examples.',
    ],
)
def test_selection_examples_with_trailing_prose_preserve_the_complete_answer(final):
    source = _transcript(final=final)
    events, receipts, failed = _deliver(source)
    assert not failed and receipts == [] and events[-1] == source[-1]


def test_multiline_example_with_prose_before_exact_selection_is_not_ambiguous():
    final = '{\n  "use_agent_answer": "foreign"\n} is an example.\n' + _SELECTION
    events, receipts, failed = _deliver(_transcript(final=final))
    assert not failed and events[-1]["result"] == "answer a"
    assert receipts[0]["status"] == "selected"


@pytest.mark.parametrize("source", ["broken", "[]", '{"type":"assistant"}'])
def test_rejects_invalid_transcript(source):
    with pytest.raises(ValueError):
        output.deliver_events(source)


@pytest.mark.parametrize("mode", ["text", "json", "stream-json"])
@pytest.mark.parametrize("redirected", [False, True])
def test_subprocess_preserves_input_stderr_and_renders_selected_output(
    tmp_path, capfd, mode, redirected
):
    source = tmp_path / "source.jsonl"
    source.write_text(_stdout(_transcript()), encoding="utf-8")
    marker = tmp_path / output.CAPABILITY_FILE
    script = (
        "import sys; from pathlib import Path; "
        "assert sys.stdin.buffer.read() == b'caller input'; "
        f"assert Path({str(marker)!r}).is_file(); "
        f"sys.stdout.write(Path({str(source)!r}).read_text(encoding='utf-8')); "
        "sys.stderr.write('child diagnostic\\n')"
    )
    real_stdout = sys.stdout
    try:
        if redirected:
            redirect_output_to_stderr()
        rc = output.run(
            [sys.executable, "-c", script, "--output-format", mode],
            session_path=tmp_path / "env.json",
            stdin_prompt=b"caller input",
        )
    finally:
        sys.stdout = real_stdout
    captured = capfd.readouterr()
    assert rc == 0 and captured.err.splitlines() == ["child diagnostic"]
    if mode == "text":
        assert captured.out.splitlines() == ["answer a"]
    elif mode == "json":
        assert json.loads(captured.out)["result"] == "answer a"
    else:
        assert json.loads(captured.out.splitlines()[-1])["result"] == "answer a"
    receipts = json.loads((tmp_path / "claude-answer-delivery-receipts.json").read_text())
    assert receipts[0]["status"] == "selected" and not marker.exists()


@pytest.mark.parametrize(
    "flags",
    [
        ["--input-format", "stream-json"],
        ["--input-format=stream-json"],
    ],
)
def test_stream_input_bypasses_delivery_and_preserves_native_output(tmp_path, capfd, flags):
    marker = tmp_path / output.CAPABILITY_FILE
    script = f"from pathlib import Path; assert not Path({str(marker)!r}).exists(); print('native output')"
    rc = output.run(
        [sys.executable, "-c", script, *flags],
        session_path=tmp_path / "env.json",
        stdin_prompt=None,
    )
    assert rc == 0 and capfd.readouterr().out.splitlines() == ["native output"]
    assert not (tmp_path / "claude-answer-delivery-receipts.json").exists()


def test_flags_after_separator_are_prompt_text():
    argv = ["/claude", "-p", "--", "--input-format=stream-json", "--output-format=json"]
    assert not output.has_streaming_input(argv)
    rewritten, requested, _ = output.stream_arguments(argv)
    assert requested == "text" and rewritten[-3:] == argv[-3:]


def test_nonzero_exit_is_preserved_and_capability_is_cleaned_up(tmp_path, capfd):
    rc = output.run(
        [sys.executable, "-c", "raise SystemExit(7)"],
        session_path=tmp_path / "env.json",
        stdin_prompt=None,
    )
    assert rc == 7 and "no terminal result" in capfd.readouterr().err
    assert not (tmp_path / output.CAPABILITY_FILE).exists()
