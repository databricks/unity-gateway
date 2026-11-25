"""User-authored skill input and native rollout evidence for the desktop journeys."""

import time
import tomllib
from pathlib import Path

from .agents.codex import completed_task_models
from .evidence import agent_sessions, is_child_session

MCP_SERVICE = "system.ai.web_search"
MCP_SERVER = "system-ai-web_search"
SKILL_NAME = "ug-desktop-search"
SEARCH_APPROVAL = f'Allow the {MCP_SERVER} MCP server to run tool "web_search"?'


def configure_databricks_token_profile(session, workspace: str) -> str:
    """Reuse the suite's credential through the saved-token path used by desktop connectors."""
    token = session.env.pop("DATABRICKS_BEARER").strip()
    assert token, "The desktop profile requires the existing integration bearer"
    # Use the default profile location so MCP startup needs no forwarded auth environment.
    for name in ("DATABRICKS_BEARER_COMMAND", "DATABRICKS_CONFIG_FILE"):
        session.env.pop(name, None)
    profile = "codex-desktop-ci"
    cli = session.which("databricks")
    session.run(
        "configure",
        "--host",
        workspace,
        "--profile",
        profile,
        binary=cli,
        input_text=token + "\n",
        timeout=30,
    )
    session.run("current-user", "me", "--profile", profile, binary=cli, timeout=30)
    return profile


def finish_windows_readiness_check(app, timeout: float = 60) -> bool:
    """Setup restarts the app-server; finish a failed readiness check before sending any task.

    Returns whether it pressed Try again.
    """
    error = "Couldn’t check Windows setup"
    tree = app.read_tree()
    if not any(row[0] == "ControlType.Text" and error in row[1:] for row in tree):
        return False
    app.session.record("windows-readiness-recheck.json", {"tree": tree})
    app.click("ControlType.Button", "Try again")
    deadline = time.monotonic() + timeout
    while True:
        tree = app.read_tree()
        if not any(row[0] == "ControlType.Text" and error in row[1:] for row in tree):
            return True
        if time.monotonic() >= deadline:
            app.session.record(
                "chatgpt-tree.json", {"waiting_for": "Sandbox readiness", "tree": tree}
            )
            raise AssertionError("Windows sandbox readiness still failed after its visible recheck")
        time.sleep(2)


def _windows_sandbox_mode(home: Path) -> str | None:
    try:
        text = (home / ".codex/config.toml").read_text(encoding="utf-8")
        return tomllib.loads(text).get("windows", {}).get("sandbox")
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return None


def wait_for_windows_setup(app, timeout: float = 60) -> bool:
    """The setup banner follows a readiness check that can transiently fail; finish one retry.

    Returns whether the banner is showing; setup already saved as elevated is accepted without it.
    """
    banner = "Finish Windows setup to continue"
    deadline = time.monotonic() + timeout
    rechecked = False
    while True:
        # One Try again per screen: an error that returns must fail, not be clicked away.
        rechecked = rechecked or finish_windows_readiness_check(
            app, max(0, deadline - time.monotonic())
        )
        tree = app.read_tree()
        if any(row[0] == "ControlType.Text" and banner in row[1:] for row in tree):
            return True
        if _windows_sandbox_mode(app.session.home) == "elevated":
            return False
        if time.monotonic() >= deadline:
            app.session.record(
                f"{app.name}-tree.json", {"waiting_for": ["ControlType.Text", banner], "tree": tree}
            )
            raise AssertionError(f"{app.name} never showed ControlType.Text {banner!r}")
        time.sleep(2)


def wait_for_windows_sandbox(app, timeout: float = 180) -> None:
    """The app saves this mode after successful setup; its success banner is transient."""
    deadline = time.monotonic() + timeout
    while True:
        mode = _windows_sandbox_mode(app.session.home)
        if mode == "elevated":
            app.session.record("windows-sandbox.json", {"mode": mode})
            return
        if time.monotonic() >= deadline:
            app.session.record(
                "chatgpt-tree.json",
                {"waiting_for": "Windows sandbox setup", "tree": app.read_tree()},
            )
            raise AssertionError(f"The app never completed elevated Windows sandbox setup: {mode}")
        time.sleep(1)


def wait_for_skill_answer(
    app, query: str, marker: str, text_role: str, button_role: str, timeout: float
) -> None:
    """Approve only the requested search through the GUI, then await its hidden answer."""
    deadline = time.monotonic() + timeout
    while True:
        tree = app.read_tree()
        labels = [label for row in tree if row[0] == text_role for label in row[1:]]
        approvals = [
            index
            for index, row in enumerate(tree)
            if row[0] == button_role and "Allow once" in row[1:]
        ]
        if approvals:
            app.session.record("mcp-approval-tree.json", {"tree": tree})
            titles = [
                index
                for index, row in enumerate(tree)
                if row[0] == text_role and SEARCH_APPROVAL in row[1:]
            ]
            assert len(approvals) == len(titles) == 1 and any(
                row[0] == text_role and query in row[1:] for row in tree[titles[0] : approvals[0]]
            ), "Unexpected tool approval; expected only web_search with the skill's query"
            app.click(button_role, "Allow once")
        elif marker in labels:
            return
        if time.monotonic() >= deadline:
            app.session.record("chatgpt-tree.json", {"waiting_for": marker, "tree": tree})
            raise AssertionError("The desktop app never displayed the completed skill answer")
        time.sleep(2)


def wait_for_submitted_prompt(session, prompt: str, timeout: float = 60) -> None:
    """Require the prompt in the native rollout before waiting for its answer."""
    deadline = time.monotonic() + timeout
    while True:
        sessions = agent_sessions(session, "codex")
        if any(
            row.get("type") == "response_item"
            and (payload := row.get("payload", {})).get("type") == "message"
            and payload.get("role") == "user"
            and any(
                part.get("type") == "input_text" and part.get("text", "").strip() == prompt
                for part in payload.get("content", [])
            )
            for records in sessions.values()
            for row in records
        ):
            return
        if time.monotonic() >= deadline:
            session.record("agent-sessions.json", sessions)
            raise AssertionError("The desktop app never recorded the submitted prompt")
        time.sleep(1)


def write_search_skill(home: Path, query: str, marker: str) -> Path:
    """An ordinary user skill, not generated ug state or a substitute MCP server."""
    # CODEX_HOME is explicit in the isolated session, including on native Windows.
    skill = home / ".codex/skills" / SKILL_NAME
    reference = skill / "references/task.md"
    reference.parent.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {SKILL_NAME}\n"
        "description: Search with the configured Unity Gateway MCP service.\n---\n"
        "Read references/task.md beside this skill. It contains the search query and completion "
        f"token. Call the {MCP_SERVER} MCP server's web_search tool with that exact query. "
        "After the tool succeeds, reply with only the completion token. "
        "Do not use built-in web search or subagents.\n",
        encoding="utf-8",
    )
    reference.write_text(f"Query: {query}\nCompletion token: {marker}\n", encoding="utf-8")
    return skill


def assert_skill_mcp_turn(records: list[dict], query: str, marker: str, model: str) -> None:
    """Tie the hidden skill values and successful MCP receipts to the app's completed turn."""
    origins = [
        (row["payload"].get("originator"), row["payload"].get("model_provider"))
        for row in records
        if row.get("type") == "session_meta"
    ]
    assert origins == [("Codex Desktop", "Databricks")], origins
    assert completed_task_models(records, marker) == {model}
    turns = [
        row["payload"]["turn_id"]
        for row in records
        if row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "task_complete"
        and row["payload"].get("last_agent_message", "").strip() == marker
    ]
    assert len(turns) == 1, f"Expected one completed desktop answer with the skill token: {turns}"
    calls = [
        item
        for row in records
        if row.get("type") == "event_msg"
        and (payload := row.get("payload", {})).get("type") == "item_completed"
        and payload.get("turn_id") == turns[0]
        and (item := payload.get("item", {})).get("type") == "McpToolCall"
        and item.get("server") == MCP_SERVER
        and item.get("tool") == "web_search"
    ]
    assert calls, "No native web_search receipt in the desktop's completed skill turn"
    for call in calls:
        assert call.get("arguments", {}).get("query") == query, call
        assert call.get("status") == "completed" and not call.get("error"), call
        result = call.get("result")
        assert isinstance(result, dict) and not result.get("isError"), call
        assert any(
            part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and part["text"].strip()
            for part in result.get("content", [])
        ), call


def assert_desktop_skill_mcp_session(session, query: str, marker: str, model: str) -> None:
    sessions = agent_sessions(session, "codex")
    session.record("agent-sessions.json", sessions)
    roots = {
        path: records
        for path, records in sessions.items()
        if not is_child_session("codex", path, records)
    }
    assert len(roots) == 1, f"Expected one desktop root session, got {list(roots)}"
    assert_skill_mcp_turn(next(iter(roots.values())), query, marker, model)
