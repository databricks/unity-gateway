"""Real Claude decisions with generated guidance and synthetic connector tools.

Synthetic connector results isolate skill compliance from credentials and client
reconnection. They do not establish native Desktop discovery, OAuth, or resume behavior.
"""

import json
import secrets
import shlex
from datetime import UTC, datetime

import pytest

from ucode.agents.desktop.cowork import CONNECTOR_SKILL_NAME, _write_connector_skill
from ucode.agents.desktop.cowork_hooks import idle_resume_context
from ucode.mcp_proxy import _ConnectionLogin, _McpBridge

SLACK_TOOL_NAME = "mcp__slack__search"
SLACK_TOOL = {
    "name": SLACK_TOOL_NAME,
    "description": "Search Slack messages by channel and query.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}
DISCOVERY_TOOL = {
    "name": "ToolSearch",
    "description": "Discover currently callable connector tools by service or task.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}
AUTH_TOOL_NAME = "mcp__slack__authenticate"
AUTH_TOOL = {
    "name": AUTH_TOOL_NAME,
    "description": "Sign in to Slack when the connector reports that sign-in is required.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
AUTH_REQUIRED_TOOL = {
    **AUTH_TOOL,
    "description": (
        "Sign-in is required to discover this connector's tools. "
        "Call this tool to sign in before retrying discovery."
    ),
}
WAIT_TOOL = {
    "name": "wait",
    "description": "Wait for the specified number of seconds before continuing.",
    "input_schema": {
        "type": "object",
        "properties": {"seconds": {"type": "integer"}},
        "required": ["seconds"],
        "additionalProperties": False,
    },
}


def _run_resumed_request(tmp_path, claude_messages, *, availability, skill_loaded, hook_note=False):
    _write_connector_skill(tmp_path, ["slack"])
    text = (tmp_path / CONNECTOR_SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    description = json.loads(text.splitlines()[2].removeprefix("description: "))
    body = text.split("---\n", 2)[2]
    skill_tool = {
        "name": "Skill",
        "description": "Load an available skill's full instructions.",
        "input_schema": {
            "type": "object",
            "properties": {"skill": {"type": "string"}},
            "required": ["skill"],
            "additionalProperties": False,
        },
    }
    system = (
        "You are Claude, helping the user with their work. Use tools to obtain external data.\n"
        f"Available skills:\n{CONNECTOR_SKILL_NAME}: {description}"
    )
    conversation = []
    if skill_loaded:
        conversation.extend(
            [
                {"role": "user", "content": "Please check coding-pod for desktop app docs."},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "previous_skill",
                            "name": "Skill",
                            "input": {"skill": CONNECTOR_SKILL_NAME},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "previous_skill", "content": body}
                    ],
                },
                {"role": "assistant", "content": "Ready to check coding-pod."},
            ]
        )
    note = ""
    if hook_note:
        checked_at = datetime(2026, 10, 28, 14, 2, tzinfo=UTC)
        note = (
            "<system-reminder>UserPromptSubmit hook additional context: "
            f"{idle_resume_context(['slack'], {'slack': 'ready'}, checked_at)}</system-reminder>\n"
        )
    conversation.append(
        {
            "role": "user",
            "content": (
                "<system-reminder>The session resumed after an idle break. "
                "MCP servers disconnected: slack.</system-reminder>\n"
                + note
                + "Can you check in coding-pod channel? What do I need to do for desktop app docs?"
            ),
        }
    )
    marker = "DOCS-" + secrets.token_hex(3)
    calls = []
    ready = availability == "available"
    sign_in_reported = False
    for _ in range(16):
        auth_tool = AUTH_REQUIRED_TOOL if sign_in_reported and not ready else AUTH_TOOL
        tools = [skill_tool, DISCOVERY_TOOL, auth_tool, WAIT_TOOL]
        if ready:
            tools.append(SLACK_TOOL)
        can_authenticate = sign_in_reported
        response = claude_messages(system, conversation, tools)
        content = response["content"]
        conversation.append({"role": "assistant", "content": content})
        actions = [block for block in content if block["type"] == "tool_use"]
        if not actions:
            assert response["stop_reason"] == "end_turn", response
            answer = "\n".join(block["text"] for block in content if block["type"] == "text")
            return calls, answer, marker
        assert response["stop_reason"] == "tool_use", response
        results = []
        for action in actions:
            name, arguments = action["name"], action["input"]
            calls.append((name, arguments))
            assert name in {tool["name"] for tool in tools}, "Claude guessed an unexposed tool"
            if name == "Skill":
                assert arguments == {"skill": CONNECTOR_SKILL_NAME}
                result = body
            elif name == "ToolSearch":
                if ready or availability == "deferred":
                    ready = True
                    result = json.dumps([SLACK_TOOL, AUTH_TOOL])
                elif availability == "sign-in-required":
                    sign_in_reported = True
                    result = json.dumps([AUTH_REQUIRED_TOOL])
                else:
                    result = "No matching service tools. Status: pending."
            elif name == AUTH_TOOL_NAME:
                assert can_authenticate, "Claude authenticated without a reported sign-in need"
                ready = True
                result = "Sign-in succeeded. Rediscover tools and continue the original task."
            elif name == "wait":
                pytest.fail("Claude waited for connector startup instead of using discovery")
            elif name == SLACK_TOOL_NAME:
                assert ready, "Claude guessed a tool that was not exposed"
                assert "coding-pod" in arguments["query"]
                result = (
                    f"coding-pod: Update the Desktop setup guide with documentation task {marker}."
                )
            else:
                pytest.fail(f"Claude called an unexposed tool: {name}")
            results.append({"type": "tool_result", "tool_use_id": action["id"], "content": result})
        conversation.append({"role": "user", "content": results})
    pytest.fail(f"Claude did not finish within 16 model turns; calls: {calls}")


@pytest.mark.parametrize(
    "skill_loaded", [False, True], ids=["description-only", "loaded-before-break"]
)
def test_calls_available_slack_despite_stale_disconnect_notice(
    tmp_path, claude_messages, skill_loaded
):
    """Scenario: a resumed request has a disconnect notice but a callable Slack tool.

    Expected: Claude calls Slack and answers using its result in the same turn.
    """
    calls, answer, marker = _run_resumed_request(
        tmp_path, claude_messages, availability="available", skill_loaded=skill_loaded
    )
    assert any(name == SLACK_TOOL_NAME for name, _ in calls), answer
    assert marker in answer, answer
    assert not any(name == "wait" for name, _ in calls), calls


@pytest.mark.parametrize(
    "skill_loaded", [False, True], ids=["description-only", "loaded-before-break"]
)
def test_discovers_deferred_slack_without_waiting_or_authenticating(
    tmp_path, claude_messages, skill_loaded
):
    """Scenario: only authenticate is initially exposed, but discovery returns Slack tools.

    Expected: Claude discovers and uses Slack without waiting or starting an unnecessary login.
    """
    calls, answer, marker = _run_resumed_request(
        tmp_path, claude_messages, availability="deferred", skill_loaded=skill_loaded
    )
    names = [name for name, _ in calls]
    assert "ToolSearch" in names, answer
    assert "wait" not in names, calls
    assert AUTH_TOOL_NAME not in names, calls
    assert SLACK_TOOL_NAME in names, answer
    assert names.index("ToolSearch") < names.index(SLACK_TOOL_NAME), calls
    assert marker in answer, answer


@pytest.mark.parametrize(
    "skill_loaded", [False, True], ids=["description-only", "loaded-before-break"]
)
def test_reports_missing_tools_and_asks_user_to_retry(tmp_path, claude_messages, skill_loaded):
    """Scenario: a resumed session never exposes Slack tools.

    Expected: Claude discovers, reports missing tools and asks the user to retry without
    waiting, polling, or authenticating just because the authenticate tool is exposed.
    """
    calls, answer, _ = _run_resumed_request(
        tmp_path, claude_messages, availability="pending", skill_loaded=skill_loaded
    )
    names = [name for name, _ in calls]
    assert "wait" not in names, calls
    assert AUTH_TOOL_NAME not in names, calls
    assert names.count("ToolSearch") == 1, (calls, answer)
    assert SLACK_TOOL_NAME not in names, calls
    assert "tools" in answer.lower(), answer
    assert any(phrase in answer.lower() for phrase in ("try again", "retry", "request again")), (
        answer
    )
    assert not any(
        claim in answer.lower()
        for claim in ("has disconnected", "need to reconnect", "token expired", "need to sign in")
    ), answer


def test_authenticates_when_discovery_reports_sign_in_required(tmp_path, claude_messages):
    """Scenario: discovery returns only authenticate, whose description says sign-in is required.

    Expected: Claude signs in once, discovers Slack tools, and completes the task.
    """
    calls, answer, marker = _run_resumed_request(
        tmp_path, claude_messages, availability="sign-in-required", skill_loaded=True
    )
    names = [name for name, _ in calls]
    assert names.count(AUTH_TOOL_NAME) == 1, (calls, answer)
    assert names.index("ToolSearch") < names.index(AUTH_TOOL_NAME) < names.index(SLACK_TOOL_NAME)
    assert "wait" not in names, calls
    assert marker in answer, answer


def test_idle_hook_note_overrides_stale_notice_before_skill_loads(tmp_path, claude_messages):
    """Scenario: after an idle break, only the skill description and the prompt hook's note
    are visible, and Slack tools are deferred until discovery.

    Expected: Claude discovers and uses Slack without trusting the disconnect notice or waiting.
    """
    calls, answer, marker = _run_resumed_request(
        tmp_path, claude_messages, availability="deferred", skill_loaded=False, hook_note=True
    )
    names = [name for name, _ in calls]
    assert "ToolSearch" in names, answer
    assert "wait" not in names, calls
    assert AUTH_TOOL_NAME not in names, calls
    assert SLACK_TOOL_NAME in names, answer
    assert marker in answer, answer


@pytest.mark.parametrize("transport", ["native-http", "proxy"])
@pytest.mark.parametrize("login_success", [True, False], ids=["success", "canceled"])
def test_code_authenticates_before_calling_cached_gmail_tools(
    tmp_path, claude_messages, transport, login_success
):
    """Scenario: a first Code request sees cached Gmail tools before sign-in.

    Expected: Claude checks auth and signs in to the requested registration, then
    summarizes the tool result; cancellation ends without a Gmail service call.
    """
    server = "system-ai-gmail"
    search_name = "mcp__system_ai_gmail__gmail_search"
    auth_name = "mcp__system_ai_gmail__authenticate"
    _write_connector_skill(tmp_path, [server], code=True)
    text = (tmp_path / CONNECTOR_SKILL_NAME / "SKILL.md").read_text()
    description = json.loads(text.splitlines()[2].removeprefix("description: "))
    body = text.split("---\n", 2)[2]
    system = (
        "You are Claude in the Desktop Code tab. Use tools to obtain external data. "
        "The installed Claude CLI executable is /opt/claude. No current MCP connection "
        "status has been provided; tool definitions may be cached.\n"
        f"Available skills:\n{CONNECTOR_SKILL_NAME}: {description}"
    )
    search = {
        "name": search_name,
        "description": "Search the user's Gmail through Unity Gateway.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
    tools = [
        search,
        DISCOVERY_TOOL,
        {
            "name": "Skill",
            "description": "Load an available skill's full instructions.",
            "input_schema": {
                "type": "object",
                "properties": {"skill": {"type": "string"}},
                "required": ["skill"],
            },
        },
        {
            "name": "Bash",
            "description": "Run a shell command in the current session environment.",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    ]
    if transport == "proxy":
        tools.append(
            {
                "name": auth_name,
                "description": "Authenticate the requested Gmail connection before using it.",
                "input_schema": {"type": "object", "properties": {}},
            }
        )
    conversation = [{"role": "user", "content": "Please summarize my Gmail from today."}]
    authenticated = False
    login_calls = 0
    service_calls = 0
    marker = "MAIL-" + secrets.token_hex(3)
    for _ in range(12):
        authenticated_before_response = authenticated
        response = claude_messages(system, conversation, tools)
        content = response["content"]
        conversation.append({"role": "assistant", "content": content})
        actions = [block for block in content if block["type"] == "tool_use"]
        if not actions:
            answer = "\n".join(block["text"] for block in content if block["type"] == "text")
            assert response["stop_reason"] == "end_turn", response
            assert login_calls == 1, (conversation, answer)
            assert bool(service_calls) == login_success, (conversation, answer)
            if login_success:
                assert marker in answer, answer
            else:
                assert answer and marker not in answer, answer
            return
        assert response["stop_reason"] == "tool_use", response
        results = []
        for action in actions:
            name, arguments = action["name"], action["input"]
            failed = False
            if name == "Skill":
                assert arguments["skill"] == CONNECTOR_SKILL_NAME
                result = body
            elif name == "ToolSearch":
                result = (
                    json.dumps([search])
                    if authenticated
                    else f"{server}: Sign-in is required. Cached tools: {search_name}."
                )
            elif name == "Bash":
                lexer = shlex.shlex(arguments["command"], posix=True, punctuation_chars=";&")
                lexer.whitespace_split = True
                commands = [[]]
                for word in lexer:
                    if word in (";", "&&"):
                        commands.append([])
                    else:
                        commands[-1].append(word)
                output = []
                for words in commands:
                    if not words:
                        continue
                    if words in (["command", "-v", "claude"], ["which", "claude"]):
                        output.append("/opt/claude")
                    else:
                        assert words[0] == "/opt/claude", words
                        command = words[1:]
                        if command == ["mcp", "list"]:
                            status = "Connected" if authenticated else "Needs authentication"
                            kind = "HTTP" if transport == "native-http" else "stdio"
                            output.append(f"{server}: ({kind}) - {status}")
                        elif command == ["mcp", "login", "--help"]:
                            output.append("Usage: claude mcp login <name>")
                        else:
                            assert transport == "native-http", command
                            assert command == ["mcp", "login", server], command
                            login_calls += 1
                            assert login_calls == 1
                            authenticated = login_success
                            failed = not login_success
                            output.append(
                                "Authentication successful." if login_success else "Login canceled."
                            )
                    if failed:
                        break
                result = "\n".join(output)
            elif name == auth_name and transport == "proxy":
                login_calls += 1
                assert login_calls == 1
                authenticated = login_success
                failed = not login_success
                result = "Authentication complete." if login_success else "Login canceled."
            elif name == search_name:
                assert authenticated_before_response, (
                    "Claude called Gmail before receiving a successful sign-in result"
                )
                service_calls += 1
                result = f"Today's Gmail: documentation task {marker} is due today."
            else:
                pytest.fail(f"Claude called an unexposed tool: {name}")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": action["id"],
                    "content": result,
                    "is_error": failed,
                }
            )
        conversation.append({"role": "user", "content": results})
    pytest.fail(f"Claude did not finish within 12 model turns: {conversation}")


@pytest.mark.parametrize("outcome", ["recovered", "persistent-error", "canceled", "no-auth-tool"])
def test_cowork_recovers_from_generic_gmail_error_once(tmp_path, claude_messages, outcome):
    """Scenario: Gmail search fails generically; authentication is deferred behind discovery.

    Expected: discover Gmail authentication, try it once, and resume only after success.
    Persistent errors, canceled login, or a missing auth tool end without a recovery loop.
    """
    _write_connector_skill(tmp_path, ["gmail"])
    skill = (tmp_path / CONNECTOR_SKILL_NAME / "SKILL.md").read_text()
    system = "You are Claude in Desktop Cowork. Follow this loaded connector skill:\n" + skill
    search_name = "mcp__gmail__gmail_search"
    search = {
        "name": search_name,
        "description": "Search the user's Gmail.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
    workspace = "https://example.databricks.com"
    bridge = _McpBridge(
        _ConnectionLogin(f"{workspace}/ai-gateway/mcp-services/system.ai.gmail", workspace, None)
    )
    generated_auth = bridge._authentication_tool()
    auth_name = "mcp__gmail__authenticate"
    auth = {
        "name": auth_name,
        "description": generated_auth["description"],
        "input_schema": generated_auth["inputSchema"],
    }
    tools = [DISCOVERY_TOOL, search]
    conversation = [{"role": "user", "content": "Please summarize my Gmail from today."}]
    generic_error = (
        "The external MCP tool failed. Check the tool input or MCP service connection, "
        "and contact the service owner if the problem persists."
    )
    marker = "MAIL-" + secrets.token_hex(3)
    service_calls = 0
    auth_calls = 0
    recovery_discovered = False
    authenticated = False
    for _ in range(12):
        authenticated_before_response = authenticated
        exposed = {tool["name"] for tool in tools}
        response = claude_messages(system, conversation, tools)
        content = response["content"]
        conversation.append({"role": "assistant", "content": content})
        actions = [block for block in content if block["type"] == "tool_use"]
        if not actions:
            assert response["stop_reason"] == "end_turn", response
            answer = "\n".join(block["text"] for block in content if block["type"] == "text")
            assert answer and recovery_discovered, conversation
            assert auth_calls == (0 if outcome == "no-auth-tool" else 1), conversation
            assert service_calls == (2 if authenticated else 1), conversation
            if outcome == "recovered":
                assert marker in answer, conversation
            else:
                assert marker not in answer, conversation
            return
        assert response["stop_reason"] == "tool_use", response
        results = []
        for action in actions:
            name = action["name"]
            assert name in exposed, "Claude guessed an unexposed tool"
            failed = False
            if name == "ToolSearch":
                recovery_discovered = recovery_discovered or service_calls > 0
                if service_calls and outcome != "no-auth-tool":
                    tools = [DISCOVERY_TOOL, search, auth]
                    result = json.dumps([search, auth])
                else:
                    result = json.dumps([search])
            elif name == auth_name:
                auth_calls += 1
                assert service_calls == 1 and auth_calls == 1, conversation
                authenticated = outcome != "canceled"
                failed = not authenticated
                result = "Authentication complete." if authenticated else "Login canceled."
            elif name == search_name:
                service_calls += 1
                assert service_calls <= 2, conversation
                if service_calls > 1:
                    assert authenticated_before_response, "Claude retried before sign-in succeeded"
                failed = service_calls == 1 or outcome == "persistent-error"
                result = (
                    generic_error if failed else f"Today's Gmail: review {marker} is due today."
                )
            else:
                pytest.fail(f"Claude called an unexpected tool: {name}")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": action["id"],
                    "content": result,
                    "is_error": failed,
                }
            )
        conversation.append({"role": "user", "content": results})
    pytest.fail(f"Claude did not finish within 12 model turns: {conversation}")
