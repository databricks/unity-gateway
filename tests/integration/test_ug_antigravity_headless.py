"""CUJs for using Google Antigravity CLI from scripts through installed ug."""

import hashlib
import json
import os
import re

import pytest
from utils.agents import antigravity
from utils.evidence import FileTask, assert_no_bearer_on_disk, fresh_id

pytestmark = [pytest.mark.live, pytest.mark.antigravity]

AGY_SETTINGS = ".gemini/antigravity-cli/settings.json"


def _configure(session, workspace):
    session.run(
        "configure",
        "--agents",
        "antigravity",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )


def _ask(session, prompt, *ug_options, command="antigravity", timeout=300):
    return session.run(
        command,
        *ug_options,
        "--",
        "-p",
        prompt,
        *antigravity.HEADLESS_ARGS,
        timeout=timeout,
    )


def _allow_tools(session, *rules):
    """A user's own Antigravity permission rules: an ordinary scenario input, kept by ug."""
    path = session.home / AGY_SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"allow": list(rules)}}, indent=2) + "\n")
    return path, path.read_bytes()


def _assert_through_ug_proxy(session):
    """Every model request went to ug's loopback proxy in gateway mode, never to Google."""
    urls = antigravity.requested_urls(session)
    assert urls, "Antigravity logged no model requests"
    assert all(
        re.match(r"http://127\.0\.0\.1:\d+/ai-gateway/gemini/v1beta/models/", url) for url in urls
    ), urls
    assert antigravity.gateway_authenticated(session)


def test_ug_antigravity_headless_completes_file_task(live_session, workspace):
    """Scenario: configure Antigravity CLI, then run a headless prompt through `ug antigravity`.

    Expected: Antigravity starts in gateway mode without Google sign-in, reads the fixture with
    its `read_file` tool, and its final answer contains the unknown value. Every model request
    goes to ug's loopback proxy, and no file under the session home holds the Databricks
    credential (the proxy keeps it in memory).
    """
    session = live_session
    task = FileTask(session)
    _configure(session, workspace)
    assert "antigravity" in session.workspace_state()["available_tools"]

    result = _ask(session, task.prompt)

    task.assert_headless_answer("antigravity", result)
    _assert_through_ug_proxy(session)
    assert_no_bearer_on_disk(session)


def test_ug_antigravity_headless_explicit_model(live_session, workspace):
    """Scenario: launch a headless task on a specific discovered Gemini model with `--model`,
    through the `ug agy` alias.

    Expected: ug accepts the Databricks model id, Antigravity runs on that model (every request
    names its Antigravity name, e.g. `gemini-3.8-flash` for `system.ai.gemini-3-8-flash`), and
    the task completes. This proves Antigravity's request, not the gateway's destination.
    """
    session = live_session
    task = FileTask(session)
    _configure(session, workspace)
    discovered = [
        model
        for model in session.workspace_state().get("gemini_models") or []
        if model.startswith("system.ai.gemini-") and "-image" not in model
    ]
    # The default is the newest; pick another so the option, not the default, decides.
    # Skip ids Antigravity knows under a `-preview` name, so the expected name is just dotted.
    candidates = [
        model
        for model in discovered[1:]
        if not re.search(r"gemini-3-flash$|gemini-3-1-(pro|flash-lite)$", model)
    ]
    model = os.environ.get("UG_INTEGRATION_ANTIGRAVITY_MODEL", "").strip() or next(
        iter(candidates), ""
    )
    assert model, f"ug configure found no second Gemini model to select: {discovered}"
    expected = re.sub(r"(?<=\d)-(?=\d)", ".", model.removeprefix("system.ai."))
    session.record("model.json", {"model": model, "antigravity_name": expected})

    result = _ask(session, task.prompt, "--model", model, command="agy")

    task.assert_headless_answer("antigravity", result)
    assert antigravity.requested_models(session) == {expected}


def test_ug_antigravity_headless_gemini_mps(
    live_session, workspace, antigravity_provider, antigravity_provider_models
):
    """Scenario: after a plain Databricks Hosted configure, run a headless task on each chosen
    target of the real Gemini Enterprise MPS with `ug antigravity --provider ... --model ...`.

    Expected: each launch routes through the provider service and completes its file task
    through ug's proxy, every Antigravity request of that launch naming the chosen target; the
    launch saves the provider for later bare launches. This proves Antigravity's requests and
    the saved routing, not the gateway's destination.
    """
    session = live_session
    _configure(session, workspace)
    assert len(antigravity_provider_models) >= 2, antigravity_provider_models

    for model in antigravity_provider_models:
        task = FileTask(session)
        before = len(antigravity.requested_urls(session))

        result = _ask(session, task.prompt, "--provider", antigravity_provider, "--model", model)

        task.assert_headless_answer("antigravity", result)
        assert antigravity.requested_models(session, since=before) == {model}
    _assert_through_ug_proxy(session)
    assert session.workspace_state()["provider_services"]["antigravity"] == antigravity_provider


def test_ug_antigravity_mcp_sql_tool(live_session, workspace):
    """Scenario: register the Databricks SQL MCP service for Antigravity with `ug mcp add`, then
    ask a headless session to run a query through it.

    The user has allowed that server's tools (and reads) in their own Antigravity settings,
    since headless runs cannot answer permission prompts.

    Expected: ug writes a stdio `ug mcp-proxy` entry to Antigravity's MCP config and keeps the
    user's settings unchanged; Antigravity calls the tool, and its answer is the query's md5
    result, which the model cannot compute itself.
    """
    session = live_session
    settings, original = _allow_tools(session, "mcp(system-ai-dbsql/*)", "read_file(*)")
    _configure(session, workspace)
    session.run("mcp", "add", "--names", "system.ai.dbsql", "--agents", "antigravity", timeout=240)
    servers = json.loads((session.home / ".gemini/config/mcp_config.json").read_text())[
        "mcpServers"
    ]
    entry = servers["system-ai-dbsql"]
    assert entry["args"][0] == "mcp-proxy", entry
    assert any("system.ai.dbsql" in arg for arg in entry["args"]), entry
    assert settings.read_bytes() == original

    value = fresh_id()
    digest = hashlib.md5(value.encode()).hexdigest()  # noqa: S324 (query output, not security)
    result = _ask(
        session,
        "Use the system-ai-dbsql MCP server's SQL tool to run exactly: "
        f"SELECT md5('{value}') AS h. Reply with only the value it returned.",
    )

    events = antigravity.stream_events(result.stdout)
    assert antigravity.completed_tools(events, "call_mcp_tool"), result.stdout
    assert digest in antigravity.final_response(events), result.stdout


def test_ug_antigravity_loads_ug_skill_directory(live_session, workspace):
    """Scenario: a user has a skill in `~/.agents/skills` (where `ug skills add` downloads) and
    their own `~/.gemini/config/skills.json` entry; they configure Antigravity and ask for the
    skill by name in a headless session.

    Antigravity discovers `.agents/skills` only inside a project, so ug declares the home
    directory in skills.json. The user allows reads in their own settings for the skill file.

    Expected: ug keeps the user's skills.json entry and adds its directory, and the final answer
    contains the code word that only the skill file holds.
    """
    session = live_session
    name = "ug-check-" + fresh_id().lower()
    code_word = fresh_id()
    skill = session.home / ".agents/skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: Gives the {name} code word. Use when asked for it.\n"
        f"---\nThe {name} code word is {code_word}.\n"
    )
    user_skills = session.home / "team-skills"
    user_skills.mkdir()
    skills_config = session.home / ".gemini/config/skills.json"
    skills_config.parent.mkdir(parents=True)
    skills_config.write_text(json.dumps({"entries": [{"path": str(user_skills)}]}))
    _allow_tools(session, "read_file(*)")

    _configure(session, workspace)
    entries = json.loads(skills_config.read_text())["entries"]
    assert entries == [
        {"path": str(user_skills)},
        {"path": str(session.home / ".agents/skills")},
    ]

    result = _ask(session, f"Use the {name} skill and reply with only the code word it gives.")

    assert code_word in antigravity.final_response(antigravity.stream_events(result.stdout))
