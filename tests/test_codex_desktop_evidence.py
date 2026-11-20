"""Reject false positives in the native desktop skill/MCP evidence checks."""

import copy
from types import SimpleNamespace

import pytest

from tests.integration.utils import codex_desktop
from tests.integration.utils.codex_desktop import (
    MCP_SERVER,
    SKILL_NAME,
    assert_skill_mcp_turn,
    write_search_skill,
)

QUERY = "Databricks Unity Catalog documentation a13b42"
MARKER = "b39c21"
MODEL = "system.ai.gpt-5-6-sol"


@pytest.mark.parametrize("failure", [None, "login", "identity"])
def test_cli_login_removes_bypasses_and_cleans_up_after_successful_login(tmp_path, failure):
    calls = []
    finalizers = []
    request = SimpleNamespace(addfinalizer=finalizers.append)
    session = SimpleNamespace(
        env={
            "HOME": str(tmp_path),
            "DATABRICKS_BEARER": "ci-token",
            "DATABRICKS_BEARER_COMMAND": "credential-broker",
            "DATABRICKS_CONFIG_FILE": "other-profile-file",
        },
        which=lambda name: tmp_path / name,
    )

    def run(*args, **kwargs):
        assert session.env == {"HOME": str(tmp_path)}
        assert kwargs["binary"] == tmp_path / "databricks"
        calls.append(args)
        if args[:2] == ("auth", "login"):
            assert kwargs["timeout"] == 150
            assert args[-2:] == ("--timeout", "2m")
            if failure == "login":
                raise AssertionError("OAuth login failed")
        elif args[:2] == ("current-user", "me") and failure == "identity":
            raise AssertionError("Identity verification failed")

    session.run = run
    if failure:
        with pytest.raises(AssertionError, match="failed"):
            codex_desktop.login_databricks_cli(session, "https://workspace.example", request)
    else:
        profile = codex_desktop.login_databricks_cli(session, "https://workspace.example", request)
        assert calls[0][2:6] == ("--host", "https://workspace.example", "--profile", profile)
        assert calls[1] == ("current-user", "me", "--profile", profile)
    if failure == "login":
        assert len(calls) == 1
        assert not finalizers
    else:
        assert len(finalizers) == 1
        finalizers[0]()
        assert calls[-1] == ("auth", "logout", "--profile", calls[0][5])


@pytest.mark.parametrize("state", ["ready", "recovers", "fails"])
def test_windows_readiness_recheck_is_visible_and_bounded(state):
    error = [["ControlType.Text", "Couldn’t check Windows setup", ""]]
    tree = [] if state == "ready" else list(error)
    clicks = []

    def click(role, label):
        clicks.append((role, label))
        if state == "recovers":
            tree.clear()

    app = SimpleNamespace(
        read_tree=lambda: tree, click=click, session=SimpleNamespace(record=lambda *args: None)
    )
    if state == "fails":
        with pytest.raises(AssertionError, match="readiness still failed"):
            codex_desktop.finish_windows_readiness_check(app, timeout=0)
    else:
        codex_desktop.finish_windows_readiness_check(app, timeout=0)
    assert clicks == ([] if state == "ready" else [("ControlType.Button", "Try again")])


@pytest.mark.parametrize("mode", [None, "unelevated", "elevated"])
def test_windows_setup_requires_the_native_elevated_mode(tmp_path, mode):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(f'[windows]\nsandbox = "{mode}"\n' if mode else "")
    evidence = {}
    app = SimpleNamespace(
        read_tree=lambda: [],
        session=SimpleNamespace(
            home=tmp_path, record=lambda name, value: evidence.update({name: value})
        ),
    )
    if mode == "elevated":
        codex_desktop.wait_for_windows_sandbox(app, timeout=0)
        assert evidence["windows-sandbox.json"] == {"mode": "elevated"}
    else:
        with pytest.raises(AssertionError, match="never completed elevated Windows sandbox setup"):
            codex_desktop.wait_for_windows_sandbox(app, timeout=0)
        assert "chatgpt-tree.json" in evidence


@pytest.mark.parametrize(
    ("text_role", "button_role"),
    [("AXStaticText", "AXButton"), ("ControlType.Text", "ControlType.Button")],
)
@pytest.mark.parametrize("invalid", [None, "server", "tool", "query", "echoed_query", "ambiguous"])
def test_gui_approves_only_the_requested_search(monkeypatch, text_role, button_role, invalid):
    approval = codex_desktop.SEARCH_APPROVAL
    if invalid == "server":
        approval = approval.replace(MCP_SERVER, "other-server")
    if invalid == "tool":
        approval = approval.replace('"web_search"', '"other-tool"')
    tree = [
        [text_role, "", approval],
        [text_role, "", "query"],
        [text_role, "", "other query" if invalid in {"query", "echoed_query"} else QUERY],
        [button_role, "Allow once", ""],
    ]
    if invalid == "ambiguous":
        tree.append([button_role, "Allow once", ""])
    if invalid == "echoed_query":
        tree.insert(0, [text_role, "", QUERY])
    clicks = []

    def click(role, label):
        clicks.append((role, label))
        tree[:] = [[text_role, "", MARKER]]

    app = SimpleNamespace(
        read_tree=lambda: tree,
        click=click,
        session=SimpleNamespace(record=lambda *args: None),
    )
    monkeypatch.setattr(codex_desktop.time, "sleep", lambda seconds: None)
    if invalid:
        with pytest.raises(AssertionError, match="Unexpected tool approval"):
            codex_desktop.wait_for_skill_answer(app, QUERY, MARKER, text_role, button_role, 1)
        assert not clicks
    else:
        codex_desktop.wait_for_skill_answer(app, QUERY, MARKER, text_role, button_role, 1)
        assert clicks == [(button_role, "Allow once")]


@pytest.mark.parametrize(
    ("kind", "role", "text", "submitted"),
    [
        ("response_item", "user", "Use the skill.\n", True),
        ("response_item", "assistant", "Use the skill.", False),
        ("response_item", "user", "Another prompt.", False),
        ("event_msg", "user", "Use the skill.", False),
    ],
)
def test_submission_requires_the_native_user_prompt(monkeypatch, kind, role, text, submitted):
    sessions = {
        "desktop": [
            {
                "type": kind,
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        ]
    }
    monkeypatch.setattr(codex_desktop, "agent_sessions", lambda *args: sessions)
    evidence = {}
    session = SimpleNamespace(record=lambda name, value: evidence.update({name: value}))
    if submitted:
        codex_desktop.wait_for_submitted_prompt(session, "Use the skill.", timeout=0)
    else:
        with pytest.raises(AssertionError, match="never recorded the submitted prompt"):
            codex_desktop.wait_for_submitted_prompt(session, "Use the skill.", timeout=0)
        assert evidence["agent-sessions.json"] == sessions


@pytest.fixture
def records():
    return [
        {
            "type": "session_meta",
            "payload": {"originator": "Codex Desktop", "model_provider": "Databricks"},
        },
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": MODEL}},
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "McpToolCall",
                    "id": "call-1",
                    "server": MCP_SERVER,
                    "tool": "web_search",
                    "arguments": {"query": QUERY},
                    "status": "completed",
                    "result": {"content": [{"type": "text", "text": "Databricks documentation"}]},
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": MARKER,
            },
        },
    ]


def test_skill_values_are_only_in_the_reference_file(tmp_path):
    skill = write_search_skill(tmp_path, QUERY, MARKER)

    assert skill == tmp_path / ".codex/skills" / SKILL_NAME
    instructions = (skill / "SKILL.md").read_text()
    assert "references/task.md" in instructions and MCP_SERVER in instructions
    assert QUERY not in instructions and MARKER not in instructions
    assert (skill / "references/task.md").read_text() == (
        f"Query: {QUERY}\nCompletion token: {MARKER}\n"
    )


def test_native_mcp_receipt_proves_execution_including_code_mode(records):
    # Native receipts establish execution even when functions.exec hides the direct model call.
    assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)
    extra_call = copy.deepcopy(records[2])
    extra_call["payload"]["item"]["id"] = "call-2"
    records.insert(3, extra_call)
    assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


@pytest.mark.parametrize(
    ("index", "changes"),
    [
        (0, {"originator": "codex_cli_rs"}),
        (0, {"model_provider": "OpenAI"}),
        (1, {"model": "another-model"}),
        (1, {"turn_id": "another-turn"}),
        (2, {"turn_id": "earlier-turn"}),
        (2, {"type": "item_started"}),
        (3, {"type": "agent_message"}),
        (3, {"last_agent_message": f"I could not finish {MARKER}"}),
    ],
)
def test_wrong_origin_model_turn_or_answer_fails(records, index, changes):
    records[index]["payload"].update(changes)
    with pytest.raises(AssertionError):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "WebSearch"},
        {"server": "another-server"},
        {"tool": "another-tool"},
        {"arguments": {"query": "an unrelated query"}},
        {"status": "inProgress"},
        {"status": "failed"},
        {"error": {"message": "permission denied"}},
        {"result": None},
        {"result": {"isError": True, "content": [{"type": "text", "text": "error"}]}},
        {"result": {"content": []}},
        {"result": {"content": [{"type": "text", "text": " "}]}},
    ],
)
def test_every_matching_mcp_receipt_must_succeed(records, changes):
    records[2]["payload"]["item"].update(changes)
    with pytest.raises(AssertionError):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


def test_success_cannot_hide_another_failed_matching_call(records):
    failed_call = copy.deepcopy(records[2])
    failed_call["payload"]["item"].update(id="failed-call", status="failed")
    records.insert(3, failed_call)
    with pytest.raises(AssertionError):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)


def test_final_answer_alone_cannot_prove_tool_use(records):
    del records[2]
    with pytest.raises(AssertionError, match="No native web_search receipt"):
        assert_skill_mcp_turn(records, QUERY, MARKER, MODEL)
