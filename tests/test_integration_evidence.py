"""Unit checks for interpreting terminal evidence, not live agent substitutes."""

import json
import re
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from tests.integration.utils import evidence
from tests.integration.utils.agents import antigravity, claude, codex, copilot
from tests.integration.utils.evidence import (
    FileTask,
    SubagentCalculation,
    assert_no_terminal_api_error,
    assistant_answer_contains,
    tool_outputs,
)
from tests.integration.utils.terminal import (
    codex_command_approval_pending,
    codex_session_footer_shown,
    codex_trust_dialog_shown,
)


@pytest.mark.parametrize(
    ("command", "environment", "missing", "expected"),
    [
        ("ug codex --disable-smart-routing", "local", None, True),
        ("ug codex --enable-smart-routing", "local", None, None),
        ("ug codex --disable-smart-routing", "remote", None, None),
        ("ug codex --disable-smart-routing", "localhost", None, None),
        ("ug codex --disable-smart-routing", "local", "prompt", False),
        ("ug codex --disable-smart-routing", "local", "footer", False),
        ("ug codex --disable-smart-routing", "local", "current-footer", False),
        ("ug codex --disable-smart-routing", "local", "choice", None),
    ],
    ids=[
        "exact",
        "wrong-command",
        "remote",
        "not-local",
        "no-prompt",
        "no-footer",
        "old-footer",
        "no-choice",
    ],
)
@pytest.mark.parametrize("command_gap", ["\n", "\n\n\n"])
def test_codex_approval_requires_the_exact_local_command(
    command, environment, missing, expected, command_gap
):
    prompt = "Would you like to run the following command?"
    footer = "Press enter to confirm or esc to cancel"
    choice = "› 1. Yes, proceed (y)"
    screen = f"{prompt}\nEnvironment: {environment}\n  $ {command}{command_gap}{choice}\n{footer}"
    if missing == "prompt":
        screen = screen.replace(prompt, "")
    elif missing == "footer":
        screen = screen.replace(footer, "")
    elif missing == "current-footer":
        screen = screen + "\n" + screen.replace(footer, "")
    elif missing == "choice":
        screen = screen.replace(choice, "")

    if expected is None:
        with pytest.raises(AssertionError, match="unexpected command approval"):
            codex_command_approval_pending(screen, "ug codex --disable-smart-routing")
    else:
        assert (
            codex_command_approval_pending(screen, "ug codex --disable-smart-routing") is expected
        )


# Codex 0.156.0 folder-trust screens captured in CI run 42283317759. Windows ConPTY rendered the
# selection marker and the footer separator as U+FFFD.
_TRUST_TEXT = (
    "  Trust this folder? Codex can read, edit, and run files here, subject to your permission "
    "settings. Folder settings can run code\n"
    "  automatically, even without a model request. Continue only if you trust these files. "
    "Your trust decision will be saved.\n"
)
_LINUX_CWD = PurePosixPath("/tmp/ug-integration-project-pf6kf82t/project with spaces")
_LINUX_TRUST = (
    f"  Folder access\n  {_LINUX_CWD}\n\n{_TRUST_TEXT}\n"
    "› 1. Trust and continue\n  2. Quit\n\n  enter continue · esc quit"
)
_WINDOWS_CWD = PureWindowsPath(
    r"D:\a\_temp\ug-integration\ug-integration-project-da67c71c70c8\project with spaces"
)
_WINDOWS_TRUST = (
    f"  Folder access\n  {_WINDOWS_CWD}\n{_TRUST_TEXT}"
    "\ufffd 1. Trust and continue\n  2. Quit\n  enter continue \ufffd esc quit"
)


@pytest.mark.parametrize(
    ("screen", "cwd", "change", "shown"),
    [
        (_LINUX_TRUST, _LINUX_CWD, None, True),
        (_WINDOWS_TRUST, _WINDOWS_CWD, None, True),
        (_LINUX_TRUST, _LINUX_CWD, ("project with spaces", "other project"), False),
        (_WINDOWS_TRUST, _WINDOWS_CWD, ("project with spaces", "other project"), False),
        (_LINUX_TRUST, _LINUX_CWD, ("› 1.", "  1."), False),
        (_WINDOWS_TRUST, _WINDOWS_CWD, ("\ufffd 1.", "  1."), False),
        (_LINUX_TRUST, _LINUX_CWD, ("Folder access", "Folder"), False),
    ],
)
def test_codex_trust_dialog_requires_the_test_project(screen, cwd, change, shown):
    if change:
        screen = screen.replace(*change)
    assert codex_trust_dialog_shown(screen, cwd) is shown


# Codex 0.156.0's ready screen captured in CI run 42294239789; its footer ends in a warning badge.
_FOOTER_CWD = PurePosixPath("/tmp/ug-integration-project-jhrdmhry/project with spaces")
_CODEX_0_156_READY = (
    "╭───────────────────────────────────────────╮\n"
    "│ >_ OpenAI Codex (v0.156.0)                │\n"
    "│                                           │\n"
    "│ model:     GPT-5.6-Sol   /model to change │\n"
    "│ directory: /tmp/…/project with spaces     │\n"
    "╰───────────────────────────────────────────╯\n"
    "\n"
    "  Tip: Use /feedback to send logs to the maintainers when something looks off.\n"
    "\n"
    "\n"
    "› Ask Codex to do anything\n"
    "\n"
    "  GPT-5.6-Sol default · /tmp/ug-integration-project-jhrdmhry/project with spaces                                   ⚠ 1 warning · f2 to view"
)


@pytest.mark.parametrize(
    ("change", "shown"),
    [
        (None, True),
        (("                                   ⚠ 1 warning · f2 to view", ""), True),
        (("⚠ 1 warning", "⚠ 2 warnings"), True),
        (("⚠ 1 warning · f2 to view", "something else"), False),
        (("project with spaces    ", "other project    "), False),
        (("GPT-5.6-Sol default · ", ""), False),
    ],
)
def test_codex_session_footer_accepts_the_warning_badge(change, shown):
    screen = _CODEX_0_156_READY.replace(*change) if change else _CODEX_0_156_READY
    assert codex_session_footer_shown(screen, _FOOTER_CWD) is shown


@pytest.mark.parametrize(
    "missing_message",
    ['"SMART_ROUTER_ENV_UNSET"', "'MISSING_SMART_ROUTER_ENV'", '"$(Remove-Item important.txt)"'],
)
@pytest.mark.parametrize("extra_command", ["", "; Remove-Item important.txt"])
@pytest.mark.parametrize("guard_style", ["exit", "else"])
def test_codex_windows_toggle_approval_accepts_only_the_known_guard(
    extra_command, missing_message, guard_style
):
    command = '& "$env:UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing'
    branch = "; exit 2 }; &" if guard_style == "exit" else " } else { &"
    suffix = "" if guard_style == "exit" else " }"
    guard = (
        "if (-not $env:UCODE_SMART_ROUTER_PYTHON -or -not $env:UCODE_SESSION_ENV_FILE) "
        f"{{ Write-Output {missing_message}{branch}"
    )
    screen = (
        "Would you like to run the following command?\nEnvironment: local\n"
        f"  $ {guard}\n  {command[2:]}{suffix}{extra_command}\n\n\n"
        "› 1. Yes, proceed (y)\nPress enter to confirm or esc to cancel"
    )
    expected_guard = guard.replace(missing_message, '"SMART_ROUTER_ENV_UNSET"')
    expected = (command, f"{expected_guard} {command[2:]}{suffix}")
    if extra_command or "$" in missing_message:
        with pytest.raises(AssertionError, match="unexpected command approval"):
            codex_command_approval_pending(screen, expected)
    else:
        assert codex_command_approval_pending(screen, expected)


@pytest.mark.parametrize("extra_command", ["", " && touch unexpected", "; touch unexpected"])
def test_codex_posix_toggle_approval_accepts_only_the_known_guard(extra_command):
    command = '"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing'
    guarded = (
        'test -n "$UCODE_SMART_ROUTER_PYTHON" && test -n "$UCODE_SESSION_ENV_FILE" && ' + command
    )
    screen = (
        "Would you like to run the following command?\nEnvironment: local\n"
        f"  $ {guarded}{extra_command}\n\n"
        "› 1. Yes, proceed (y)\nPress enter to confirm or esc to cancel"
    )
    if extra_command:
        with pytest.raises(AssertionError, match="unexpected command approval"):
            codex_command_approval_pending(screen, (command, guarded))
    else:
        assert codex_command_approval_pending(screen, (command, guarded))


class _Session:
    def __init__(self, home):
        self.home = home

    def record(self, _name, _value):
        pass


def _records(*results):
    """One native tool result per call id send-1, send-2, ..."""
    return [
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": f"send-{i}"}]},
            "toolUseResult": result,
        }
        for i, result in enumerate(results, 1)
    ]


def _call(index=1, to="child-id"):
    return {"id": f"send-{index}", "input": {} if to is None else {"to": to}}


_RESUMED = {"success": True, "resumedAgentId": "child-id"}
_QUEUED = {
    "success": True,
    "message": "Message queued for delivery to child-id at its next tool round.",
    "pin": {"id": "child-id"},
}
# Real result from run 41768149986: no pin, no resumedAgentId.
_REJECTED = {
    "success": False,
    "message": "notify_when_idle is only supported for Claude sessions on this machine in "
    "this release (not teammates, subagents, Remote Control or cloud sessions). Your "
    "message was NOT delivered; send it again without notify_when_idle if it should still go.",
}


@pytest.mark.parametrize("recipient", ["child-id", "child-name"])
def test_claude_send_resumed_accepts_id_or_name_resolved_to_original_child(recipient):
    records = [{"type": "user", "message": {"content": "An ordinary user prompt"}}]
    records += _records(_RESUMED)
    assert claude.classify_send_message(_call(to=recipient), records, "child-id") == "resumed"


def test_claude_send_queued_to_the_child():
    assert claude.classify_send_message(_call(), _records(_QUEUED), "child-id") == "queued"


def test_claude_send_rejected_for_the_child_by_to_or_pin():
    records = _records(_REJECTED)
    assert claude.classify_send_message(_call(), records, "child-id") == "rejected"
    pinned = _records({**_REJECTED, "pin": {"id": "child-id"}})
    assert claude.classify_send_message(_call(to="child-name"), pinned, "child-id") == "rejected"


@pytest.mark.parametrize(
    ("call", "result"),
    [
        (_call(to="other-child"), _REJECTED),
        (_call(), {"success": True, "resumedAgentId": "other-child"}),
        (_call(), {**_QUEUED, "pin": {"id": "other-child"}}),
        (_call(), {**_QUEUED, "message": "Delivered"}),
        (_call(), {"success": True, "message": "queued"}),
        (_call(), {"success": False, "resumedAgentId": "other-child"}),
        (_call(), "not a dict"),
        (_call(), None),
    ],
)
def test_claude_send_rejects_other_agents_and_unknown_shapes(call, result):
    with pytest.raises(AssertionError, match="neither resumed nor was queued or rejected"):
        claude.classify_send_message(call, _records(result), "child-id")


@pytest.mark.parametrize("recipient", [None, "", "  "])
def test_claude_send_missing_recipient_has_readable_assertion(recipient):
    with pytest.raises(AssertionError, match="requires a nonempty 'to' recipient"):
        claude.classify_send_message(_call(to=recipient), _records(_RESUMED), "child-id")


def test_claude_send_requires_matching_native_result():
    with pytest.raises(AssertionError, match="No native tool result for SendMessage send-2"):
        claude.classify_send_message(_call(2), _records(_RESUMED), "child-id")


def test_claude_follow_ups_tolerate_queued_and_rejected_alongside_a_resume():
    calls = [_call(1), _call(2), _call(3)]
    resumed = claude.assert_follow_ups_resumed(calls, _records(_RESUMED, _QUEUED, _REJECTED))
    assert resumed == "child-id"


@pytest.mark.parametrize("results", [(_QUEUED,), (_REJECTED,), (_QUEUED, _REJECTED)])
def test_claude_follow_ups_require_a_real_resume(results):
    calls = [_call(i) for i in range(1, len(results) + 1)]
    with pytest.raises(AssertionError, match="resume exactly one child"):
        claude.assert_follow_ups_resumed(calls, _records(*results))


def test_claude_follow_ups_fail_when_any_send_targets_another_agent():
    calls = [_call(1), _call(2, to="other-child")]
    with pytest.raises(AssertionError, match="neither resumed nor was queued or rejected"):
        claude.assert_follow_ups_resumed(calls, _records(_RESUMED, _REJECTED))


def test_claude_follow_ups_resolve_the_single_resumed_child():
    calls = [_call(1), _call(2), _call(3)]
    assert (
        claude.assert_follow_ups_resumed(calls, _records(_QUEUED, _RESUMED, _REJECTED))
        == "child-id"
    )


@pytest.mark.parametrize(
    "results",
    [
        (
            {"success": True, "resumedAgentId": "child-id"},
            {"success": True, "resumedAgentId": "other-child"},
        ),
        ({"success": True},),
    ],
)
def test_claude_follow_ups_require_exactly_one_resumed_child(results):
    calls = [_call(i) for i in range(1, len(results) + 1)]
    with pytest.raises(AssertionError, match="resume exactly one child"):
        claude.assert_follow_ups_resumed(calls, _records(*results))


def test_claude_follow_ups_require_a_native_result():
    with pytest.raises(AssertionError, match="No native tool result for SendMessage send-1"):
        claude.assert_follow_ups_resumed([_call()], [])


def _transcript_session(home, agent, transcripts):
    directory = home / {"claude": ".claude/projects", "codex": ".codex/sessions"}[agent]
    for name, records in transcripts.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return _Session(home)


def _claude_peer_delivery(child_id, value):
    body = "[Subagent hand-back] The report follows:\n  " + value
    return {
        "type": "user",
        "isMeta": True,
        "message": {
            "role": "user",
            "content": f'Another Claude session sent a message:\n<agent-message from="{child_id}">\n'
            f"{body}\n</agent-message>",
        },
        "origin": {"kind": "peer", "from": child_id, "senderTaskId": child_id, "body": body},
    }


def _claude_child_handback(value):
    return {
        "type": "assistant",
        "isSidechain": True,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "SubagentHandback",
                    "id": "handback-1",
                    "input": {"message": value},
                }
            ],
        },
    }


def _claude_parent_text(value):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": value}]},
    }


def test_claude_child_answer_requires_delivery_even_when_parent_hallucinates():
    value = "3d3faaf02749"
    fabricated = _claude_parent_text(
        'Waiting for the subagent... <task-notification>"a fabricated quote"</task-notification>'
    )
    parent = "project/session.jsonl"
    child = "project/session/subagents/agent-ae73fb7e161b59bbb.jsonl"
    sessions = {parent: [fabricated], child: [_claude_child_handback(value)]}
    assert not claude.claude_delivered_child_answers(sessions, value)
    sessions[parent].append(_claude_peer_delivery("ae73fb7e161b59bbb", value))
    assert claude.claude_delivered_child_answers(sessions, value) == {"ae73fb7e161b59bbb"}


def test_claude_child_delivery_ignores_prompt_and_tool_input_echoes():
    value = "3d3faaf02749"
    parent = "project/session.jsonl"
    child = "project/session/subagents/agent-child-id.jsonl"
    sessions = {
        parent: [
            {"type": "user", "message": {"role": "user", "content": value}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Agent", "input": {"prompt": value}}],
                },
            },
        ],
        child: [_claude_child_handback(value)],
    }
    assert not claude.claude_delivered_child_answers(sessions, value)
    sessions[parent].append(_claude_peer_delivery("other-child", value))
    assert not claude.claude_delivered_child_answers(sessions, value)
    sessions[parent].append(_claude_parent_text(value))
    assert claude.claude_delivered_child_answers(sessions, value) == {"child-id"}
    sessions[child] = [{"type": "user", "message": {"role": "user", "content": value}}]
    assert not claude.claude_delivered_child_answers(sessions, value)


def test_claude_child_delivery_must_belong_to_same_parent_session():
    value = "3d3faaf02749"
    sessions = {
        "project/other.jsonl": [_claude_peer_delivery("child-id", value)],
        "project/session.jsonl": [],
        "project/session/subagents/agent-child-id.jsonl": [_claude_child_handback(value)],
    }
    assert not claude.claude_delivered_child_answers(sessions, value)


def _write_answer(home, agent, *, child, value):
    if agent == "claude":
        directory = home / ".claude/projects/project"
        if child:
            directory /= "subagents"
        record = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": value}]},
        }
    else:
        directory = home / ".codex/sessions"
        source = {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}
        records = [
            {"type": "session_meta", "payload": {"source": source if child else "cli"}},
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": value},
            },
        ]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ("child.jsonl" if child else "parent.jsonl")).write_text(
            "".join(json.dumps(row) + "\n" for row in records)
        )
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ("child.jsonl" if child else "parent.jsonl")).write_text(json.dumps(record) + "\n")


@pytest.mark.parametrize(
    "screen",
    [
        "■ exceeded retry limit, last status: 429 Too Many Requests",
        "■ unexpected status 403 Forbidden: PERMISSION_DENIED",
        "■ unexpected status 401 Unauthorized",
    ],
)
def test_terminal_api_failure_reports_the_actual_error(screen):
    with pytest.raises(AssertionError, match="Agent returned a terminal API error") as error:
        assert_no_terminal_api_error(screen)
    assert screen in str(error.value)


@pytest.mark.parametrize(
    "screen",
    [
        "Reconnecting... 1/5 (unexpected status 429 Too Many Requests)",
        "Reconnecting... 1/5 (unexpected status 503 Service Unavailable)",
        "Working (5s · esc to interrupt)",
    ],
)
def test_transient_retries_and_running_tasks_are_not_terminal_errors(screen):
    assert_no_terminal_api_error(screen)


@pytest.mark.parametrize("reason", ["RESOURCE_EXHAUSTED", "REQUEST_LIMIT_EXCEEDED"])
def test_antigravity_resource_exhausted_detects_provider_429(reason):
    stdout = json.dumps(
        {
            "event": "result",
            "result": {
                "status": "ERROR",
                "error": f"request failed (code 429): {reason}",
            },
        }
    )

    assert antigravity.resource_exhausted(stdout)


@pytest.mark.parametrize(
    "result",
    [
        {"status": "SUCCESS", "response": "RESOURCE_EXHAUSTED code 429"},
        {"status": "ERROR", "error": "request failed (code 500): RESOURCE_EXHAUSTED"},
        {"status": "ERROR", "error": "request failed (code 429): PERMISSION_DENIED"},
    ],
)
def test_antigravity_resource_exhausted_rejects_non_retryable_results(result):
    stdout = json.dumps({"event": "result", "result": result})

    assert not antigravity.resource_exhausted(stdout)


def _antigravity_web_search_events():
    return [
        {
            "event": "step_update",
            "step_update": {
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "call_mcp_tool",
                "tool_info": {
                    "name": "call_mcp_tool",
                    "parameters": {
                        "ServerName": "system-ai-web_search",
                        "ToolName": "web_search",
                        "Arguments": {"query": "Databricks official website"},
                    },
                    "output": "Official site: https://www.databricks.com/",
                },
            },
        },
        {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "https://www.databricks.com/"},
        },
    ]


@pytest.mark.parametrize("encoded", [False, True])
def test_antigravity_web_search_requires_native_result_and_matching_answer(encoded):
    events = _antigravity_web_search_events()
    if encoded:
        call = events[0]["step_update"]["tool_info"]
        call["output"] = json.dumps(
            {"isError": False, "content": [{"type": "text", "text": call["output"]}]}
        )
    antigravity.assert_mcp_web_search_answer(
        events, server="system-ai-web_search", query="Databricks official website"
    )


@pytest.mark.parametrize(
    ("output", "answer"),
    [
        (
            "Databricks' official website is: **https://www.databricks.com/** "
            "([databricks.com](https://www.databricks.com//?utm_source=openai))",
            "https://www.databricks.com/",
        ),
        ("*https://www.databricks.com/*", "https://www.databricks.com/"),
        ("`https://www.databricks.com/`", "https://www.databricks.com/"),
        ("https://www.databricks.com/", "**https://www.databricks.com/**"),
        ("https://www.databricks.com/", "`https://www.databricks.com/`"),
        ("[Databricks](https://www.databricks.com/)", "<https://www.databricks.com/>"),
        ("**https://www.databricks.com/**.", "https://www.databricks.com/"),
        ("*https://www.databricks.com/*,", "https://www.databricks.com/"),
        ("`https://www.databricks.com/`.", "https://www.databricks.com/"),
    ],
)
def test_antigravity_web_search_matches_urls_inside_markdown(output, answer):
    events = _antigravity_web_search_events()
    events[0]["step_update"]["tool_info"]["output"] = output
    events[1]["result"]["response"] = answer

    antigravity.assert_mcp_web_search_answer(
        events, server="system-ai-web_search", query="Databricks official website"
    )


@pytest.mark.parametrize(
    ("output", "answer"),
    [
        ("https://www.databricks.com//?utm_source=openai", "https://www.databricks.com/\n"),
        (
            "[Databricks](https://www.databricks.com//?utm_source=openai)",
            "https://www.databricks.com/",
        ),
        ("https://www.databricks.com/", "https://www.databricks.com/?utm_source=openai"),
        (
            "https://www.databricks.com/?utm_source=openai&q=a#top",
            "https://www.databricks.com/?q=a#top",
        ),
    ],
)
def test_antigravity_web_search_matches_homepage_tracking_urls(output, answer):
    events = _antigravity_web_search_events()
    events[0]["step_update"]["tool_info"]["output"] = output
    events[1]["result"]["response"] = answer

    antigravity.assert_mcp_web_search_answer(
        events, server="system-ai-web_search", query="Databricks official website"
    )


@pytest.mark.parametrize(
    ("output", "answer"),
    [
        ("**https://www.databricks.com/a**", "https://www.databricks.com/b"),
        ("**https://www.databricks.com/?q=a**", "https://www.databricks.com/?q=b"),
        ("**https://www.databricks.com/#a**", "https://www.databricks.com/#b"),
        ("https://www.databricks.com/?q=*", "https://www.databricks.com/?q="),
        ("**https://www.databricks.com/?q=*.**", "https://www.databricks.com/?q=*"),
        ("https://www.databricks.com/?q=.", "https://www.databricks.com/?q="),
        ("https://www.databricks.com//?utm_source=openai", "https://www.databricks.com/a"),
        ("https://www.databricks.com/?utm_source=openai&q=a", "https://www.databricks.com/?q=b"),
        ("https://www.databricks.com/?utm_source=openai#top", "https://www.databricks.com/"),
        ("https://www.databricks.com/", "http://www.databricks.com/"),
        ("https://www.databricks.com/", "https://databricks.com/"),
        ("https://www.databricks.com/", "https://www.databricks.com.example.org/"),
    ],
)
def test_antigravity_web_search_preserves_url_path_query_and_fragment(output, answer):
    events = _antigravity_web_search_events()
    events[0]["step_update"]["tool_info"]["output"] = output
    events[1]["result"]["response"] = answer

    with pytest.raises(AssertionError):
        antigravity.assert_mcp_web_search_answer(
            events, server="system-ai-web_search", query="Databricks official website"
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ((0, "step_update", "state"), "ACTIVE"),
        ((0, "step_update", "state"), "ERROR"),
        ((0, "step_update", "tool_name"), "search_web"),
        ((0, "step_update", "tool_info", "parameters", "ServerName"), "another-server"),
        ((0, "step_update", "tool_info", "parameters", "ToolName"), "execute_sql"),
        ((0, "step_update", "tool_info", "parameters", "Arguments", "query"), "another query"),
        ((0, "step_update", "tool_info", "error"), {"type": "TOOL_ERROR"}),
        ((0, "step_update", "tool_info", "output"), ""),
        ((0, "step_update", "tool_info", "output"), "https://www.databricks.com.example.org/"),
        (
            (0, "step_update", "tool_info", "output"),
            '{"isError": true, "content": [{"type": "text", "text": "https://www.databricks.com/"}]}',
        ),
        ((1, "result", "status"), "ERROR"),
        ((1, "result", "response"), ""),
        ((1, "result", "response"), "https://www.databricks.com/not-returned-by-search"),
        ((1, "result", "denied_actions"), [{"action": "command", "display_name": "RunCommand"}]),
    ],
)
def test_antigravity_web_search_rejects_incomplete_or_unrelated_evidence(path, value):
    events = _antigravity_web_search_events()
    target = events
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(AssertionError):
        antigravity.assert_mcp_web_search_answer(
            events, server="system-ai-web_search", query="Databricks official website"
        )


def test_antigravity_resource_exhausted_retry_delay_is_truncated_exponential(monkeypatch):
    monkeypatch.setattr(antigravity.random, "uniform", lambda _start, _end: 0)

    assert [antigravity.resource_exhausted_retry_delay(index) for index in range(5)] == [
        5,
        10,
        20,
        20,
        20,
    ]


def test_tagged_calculation_uses_a_short_native_task_name():
    task = SubagentCalculation("1+2", "3")

    assert re.fullmatch(r"[a-z][a-z0-9_]{0,11}", task.marker)


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("child", [False, True])
def test_tagged_calculation_rejects_mistyped_marker_and_wrong_result(tmp_path, agent, child):
    session = _Session(tmp_path)
    task = SubagentCalculation("1+2", "3")

    for answer in (task.marker[:-1] + "=3", task.marker + "=4"):
        _write_answer(tmp_path, agent, child=child, value=answer)

        assert not task.completed(session, agent, child=child)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_tagged_calculation_requires_the_native_child_answer(tmp_path, agent):
    session = _Session(tmp_path)
    task = SubagentCalculation("1+1", "2")
    _write_answer(tmp_path, agent, child=False, value=task.value)

    assert task.completed(session, agent)
    assert not task.completed(session, agent, child=True)

    _write_answer(tmp_path, agent, child=True, value=task.value)

    assert task.completed(session, agent, child=True)
    assert assistant_answer_contains(session, agent, task.value, child=True)
    assert task.marker in task.prompt
    assert f'task name "{task.marker}"' in task.prompt
    assert "1+1" in task.prompt


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("child", [False, True])
def test_file_task_child_completion_is_independent_of_parent_answer(tmp_path, agent, child):
    session = _Session(tmp_path)
    session.cwd = tmp_path
    task = FileTask(session)
    assert task.value not in task.delegate_prompt
    assert not task.completed(session, agent, child=True)
    _write_answer(tmp_path, agent, child=child, value=task.value)
    assert task.completed(session, agent, child=True) == child
    assert task.completed(session, agent) != child


def test_codex_model_identity_uses_only_the_completed_answer_turn():
    records = [
        {
            "type": "turn_context",
            "payload": {"turn_id": "other", "model": "catalog.other_models.codex_decoy"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "matching", "model": "catalog.models.gpt_luna"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "matching",
                "last_agent_message": "withheld-file-value",
            },
        },
    ]
    assert codex.completed_task_models(records, "withheld-file-value") == {
        "catalog.models.gpt_luna"
    }


def test_codex_model_identity_rejects_prompt_only_evidence():
    records = [
        {
            "type": "turn_context",
            "payload": {"turn_id": "matching", "model": "catalog.models.gpt_luna"},
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "withheld-file-value"},
        },
    ]
    assert codex.completed_task_models(records, "withheld-file-value") == set()


def test_copilot_model_identity_uses_only_answering_assistant_messages():
    records = [
        {"type": "user.message", "data": {"content": "withheld-file-value"}},
        {
            "type": "assistant.message",
            "data": {"content": "Reading the file.", "model": "system.ai.claude-haiku-4-5"},
        },
        {
            "type": "assistant.message",
            "data": {"content": "withheld-file-value", "model": "system.ai.gpt-5-4-nano"},
        },
        {"type": "assistant.message"},
        {"type": "assistant.message", "data": {"content": None, "model": "system.ai.gpt-5"}},
    ]
    assert copilot.completed_task_models(records, "withheld-file-value") == {
        "system.ai.gpt-5-4-nano"
    }
    assert copilot.assistant_answers(records) == ["Reading the file.", "withheld-file-value"]


def test_copilot_model_identity_rejects_an_answer_without_a_model():
    records = [{"type": "assistant.message", "data": {"content": "withheld-file-value"}}]
    with pytest.raises(AssertionError, match="completed-task model"):
        copilot.completed_task_models(records, "withheld-file-value")


def test_copilot_selected_models_follow_start_then_switches():
    records = [
        {"type": "session.start", "data": {"selectedModel": "dbx-anthropic/claude"}},
        {"type": "session.model_change", "data": {"newModel": "dbx-responses/gpt"}},
        {"type": "session.model_change", "data": {}},
        {"type": "assistant.message", "data": {"model": "system.ai.gpt"}},
    ]
    assert copilot.selected_models(records) == ["dbx-anthropic/claude", "dbx-responses/gpt"]


def test_claude_model_identity_uses_assistant_answer_not_tool_output():
    records = [
        {
            "type": "user",
            "message": {
                "model": "catalog.other_models.claude_decoy",
                "content": [{"type": "text", "text": "value"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "catalog.models.claude_sonnet",
                "content": [{"type": "text", "text": "value"}],
            },
        },
    ]
    assert claude.completed_task_models(records, "value") == {"catalog.models.claude_sonnet"}


def test_claude_assistant_answers_include_subagent_handback():
    records = [
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-use",
                        "name": "SubagentHandback",
                        "input": {"message": "child answer"},
                    }
                ],
            },
        }
    ]

    assert claude.assistant_answers(records) == ["child answer"]


def test_claude_assistant_answers_ignore_parent_handback():
    records = [
        {
            "type": "assistant",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-use",
                        "name": "SubagentHandback",
                        "input": {"message": "not a child answer"},
                    }
                ],
            },
        }
    ]

    assert claude.assistant_answers(records) == []


def completed_records(agent, model, answer="value", turn_id="matching"):
    if agent == "claude":
        return [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": answer}],
                },
            }
        ]
    return [
        {"type": "turn_context", "payload": {"turn_id": turn_id, "model": model}},
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": answer},
        },
    ]


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("parent_answer", [None, "value", "unrelated-answer"])
def test_completed_task_models_excludes_child_only_and_conflicting_child_evidence(
    tmp_path, agent, parent_answer
):
    child = completed_records(agent, "child-model")
    if agent == "codex":
        child.insert(0, {"type": "session_meta", "payload": {"source": {"subagent": "spawn"}}})
    sessions = {"project/subagents/child.jsonl": child}
    if parent_answer is not None:
        sessions["project/parent.jsonl"] = completed_records(agent, "parent-model", parent_answer)
    session = _transcript_session(tmp_path, agent, sessions)
    expected = {"parent-model"} if parent_answer == "value" else set()
    assert evidence.completed_task_models(session, agent, "value") == expected


def test_codex_completed_task_models_requires_context_in_same_session(tmp_path):
    context, completion = completed_records("codex", "model")
    session = _transcript_session(
        tmp_path,
        "codex",
        {"context-session.jsonl": [context], "answer-session.jsonl": [completion]},
    )
    with pytest.raises(AssertionError, match="Missing Codex model context"):
        evidence.completed_task_models(session, "codex", "value")


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("models", [[], ["unexpected"], ["expected", "conflicting"]])
def test_completed_task_model_assertion_requires_exact_singleton(tmp_path, agent, models):
    records = [record for model in models for record in completed_records(agent, model)]
    session = _transcript_session(tmp_path, agent, {"parent.jsonl": records})
    with pytest.raises(AssertionError):
        evidence.assert_completed_task_model(session, agent, "value", "expected")


@pytest.mark.parametrize(
    "agent,record",
    [
        (
            "claude",
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "confirmed"}]},
            },
        ),
        (
            "claude",
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [{"type": "text", "text": "confirmed"}],
                        }
                    ]
                },
            },
        ),
        (
            "codex",
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "confirmed"},
            },
        ),
        (
            "codex",
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "output": [{"type": "input_text", "text": "confirmed"}],
                },
            },
        ),
    ],
)
def test_tool_outputs_read_native_string_and_block_results(agent, record):
    assert tool_outputs(agent, [record]) == ["confirmed"]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_tool_outputs_exclude_user_echoes_and_assistant_claims(agent):
    records = [
        {"type": "user", "message": {"role": "user", "content": "confirmed"}},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "confirmed"}]},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "confirmed"}],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": "confirmed", "is_error": True}]
            },
        },
    ]
    assert tool_outputs(agent, records) == []


@pytest.mark.parametrize("orchestrator_on", [False, True])
def test_codex_generic_routing_label_correlates_to_tagged_prompt(orchestrator_on):
    suffix = " [orchestrator on]" if orchestrator_on else ""
    panel = (
        "│ Using Unity Gateway Smart Router - Subagent │\n"
        f"│ Subagent : subagent{suffix} │\n"
        "│ Selected Model : gpt-6-luna │\n└──────────┘\n"
    )
    prompt = '› Spawn a child with task name "ug_subagent_ab12"\n  and return its answer.\n\n'
    screen = prompt + panel + "› Ask Codex to do anything\n"
    assert evidence.codex_routing_display_for_task(
        screen, "ug_subagent_ab12", orchestrator_on=orchestrator_on
    )
    assert not evidence.codex_routing_display_for_task(
        screen, "ug_subagent_other", orchestrator_on=orchestrator_on
    )
    assert not evidence.codex_routing_display_for_task(
        panel + prompt, "ug_subagent_ab12", orchestrator_on=orchestrator_on
    )
    assert not evidence.codex_routing_display_for_task(
        prompt + "› Another task\n\n" + panel,
        "ug_subagent_ab12",
        orchestrator_on=orchestrator_on,
    )
    assert not evidence.codex_routing_display_for_task(
        screen, "ug_subagent_ab12", orchestrator_on=not orchestrator_on
    )


def test_codex_compacted_prompt_uses_adjacent_native_spawn():
    panel = (
        "• Context compacted\n\n"
        "│ Using Unity Gateway Smart Router - Subagent │\n"
        "│ Subagent : subagent [orchestrator on] │\n"
        "│ Selected Model : gpt-6-luna │\n└──────────┘\n\n"
    )
    spawn = "• Spawned child-id (gpt-6-luna)\n  └ Task name: ug_subagent_ab12. Calculate 2+2.\n\n"
    assert evidence.codex_routing_display_for_task(
        panel + spawn, "ug_subagent_ab12", orchestrator_on=True
    )
    assert not evidence.codex_routing_display_for_task(
        panel + spawn, "ug_subagent_other", orchestrator_on=True
    )
    assert not evidence.codex_routing_display_for_task(
        panel + "• Answer: ug_subagent_ab12=4\n", "ug_subagent_ab12", orchestrator_on=True
    )
    assert not evidence.codex_routing_display_for_task(
        panel + "› New prompt\n\n" + spawn, "ug_subagent_ab12", orchestrator_on=True
    )
    assert not evidence.codex_routing_display_for_task(
        panel + "• Spawned other-child\n  └ Another task\n\n" + spawn,
        "ug_subagent_ab12",
        orchestrator_on=True,
    )


def test_claude_follow_ups_accept_repeated_resumes_of_the_same_child():
    calls = [_call(1), _call(2)]
    assert claude.assert_follow_ups_resumed(calls, _records(_RESUMED, _RESUMED)) == "child-id"


_PARENT = "/home/.claude/projects/p/parent.jsonl"


def _spawn(call_id, prompt="read input.txt"):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": call_id, "name": "Agent", "input": {"prompt": prompt}}
            ]
        },
    }


def _child(name):
    return {f"/home/.claude/projects/p/parent/subagents/agent-{name}.jsonl": [{"type": "user"}]}


def test_claude_one_delegated_task_tolerates_identical_duplicate_spawns():
    sessions = {_PARENT: [_spawn("a"), _spawn("b")], **_child("one"), **_child("two")}
    claude.assert_one_delegated_task(sessions)


@pytest.mark.parametrize(
    "sessions",
    [
        {_PARENT: [_spawn("a"), _spawn("b", "read other.txt")], **_child("one"), **_child("two")},
        {_PARENT: [_spawn("a")]},
    ],
    ids=["differently-tasked-spawn", "no-child"],
)
def test_claude_one_delegated_task_rejects_other_shapes(sessions):
    with pytest.raises(AssertionError):
        claude.assert_one_delegated_task(sessions)


def test_claude_follow_up_spawned_nothing_accepts_unchanged_children_and_spawns():
    before = {_PARENT: [_spawn("a")], **_child("one")}
    after = {_PARENT: [_spawn("a"), {"type": "user"}], **_child("one")}
    claude.assert_follow_up_spawned_nothing(before, after)


@pytest.mark.parametrize(
    "after",
    [
        {_PARENT: [_spawn("a")], **_child("one"), **_child("two")},
        {_PARENT: [_spawn("a"), _spawn("b")], **_child("one")},
    ],
    ids=["new-child", "new-spawn"],
)
def test_claude_follow_up_spawned_nothing_rejects_new_children_or_spawns(after):
    before = {_PARENT: [_spawn("a")], **_child("one")}
    with pytest.raises(AssertionError):
        claude.assert_follow_up_spawned_nothing(before, after)
