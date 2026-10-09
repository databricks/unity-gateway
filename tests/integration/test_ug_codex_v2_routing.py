"""Native Codex v2 assignments must reach the routing hook as plaintext."""

from __future__ import annotations

import json
import shlex
import sys
import tomllib
from pathlib import Path

import pytest
from utils.agents import codex
from utils.evidence import (
    FileTask,
    agent_sessions,
    assert_subagent_routed,
    is_child_session,
    read_jsonl,
)
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.codex]

CODEX_NATIVE_START_MODEL = "gpt-5.6-sol"
CODEX_NATIVE_CATALOG_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra"]


def _seed_user_owned_native_codex_settings(session) -> tuple[Path, bytes, bytes]:
    # The workspace catalog includes older models; this user-owned catalog forces v2.
    result = session.run("debug", "models", "--bundled", binary="codex", timeout=60)
    models = json.loads(result.stdout)["models"]
    selected = [model for model in models if model["slug"] in CODEX_NATIVE_CATALOG_MODELS]
    assert {model["slug"] for model in selected} == set(CODEX_NATIVE_CATALOG_MODELS)
    assert all(model.get("multi_agent_version") == "v2" for model in selected)

    codex_home = session.home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    catalog = codex_home / "native-user-model-catalog.json"
    catalog.write_text(json.dumps({"models": selected}))

    # Observe actual native stdin without modifying the hook input or result.
    observer = codex_home / "record-pre-tool-use.py"
    hook_input = codex_home / "native-pre-tool-use.jsonl"
    observer.write_text(
        "import json, sys\n"
        f"with open({str(hook_input)!r}, 'a') as output:\n"
        "    output.write(json.dumps(json.load(sys.stdin)) + '\\n')\n"
        "print('{}')\n"
    )
    config = codex_home / "config.toml"
    config.write_text(
        f"model = {json.dumps(CODEX_NATIVE_START_MODEL)}\n"
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
        "[[hooks.PreToolUse]]\n"
        'matcher = "Agent|.*spawn_agent$"\n'
        "[[hooks.PreToolUse.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(shlex.join([sys.executable, str(observer)]))}\n"
    )
    return catalog, config.read_bytes(), catalog.read_bytes()


def test_ug_codex_native_v2_plain_assignment_routing(live_session, workspace):
    """Scenario: configure ug with a user-owned native v2 catalog and observer hook,
    then delegate one multiline file task with subagent-only routing.

    Expected: the native call, actual hook input, and router decision contain the exact
    assignment. One native child completes on the selected model; the parent stays on
    its original model. The session provider works under managed Databricks settings
    and preserves the user's config. This case proves the fixed behavior, not the baseline.
    """
    session = live_session
    task = FileTask(session)
    assignment = task.prompt + "\nPreserve punctuation (!?;:) and capitalization; add no label."
    task_name = "ug_native_file"
    prompt = (
        f'Use native collaboration.spawn_agent exactly once with task_name "{task_name}". '
        "Set message to exactly the assignment between BEGIN and END, preserving the newline.\n"
        f"BEGIN\n{assignment}\nEND\n"
        "Do not read the file yourself. Wait for the child and return only its file value."
    )
    catalog, original_config, original_catalog = _seed_user_owned_native_codex_settings(session)
    session.env["ENABLE_SMART_ROUTING_V2"] = "1"
    session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    config = session.home / ".codex/config.toml"
    assert config.read_bytes() == original_config
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "codex-native-v2-routing"
    ) as tui:
        tui.boot()
        tui.submit(prompt)
        tui.wait_for(
            lambda _: task.completed(session, "codex"),
            "the parent to relay the child file value",
            timeout=300,
        )
        tui.exit_normally()

    # Native trust records may be added; user settings must remain intact.
    before, after = tomllib.loads(original_config.decode()), tomllib.loads(config.read_text())
    hooks = before.pop("hooks")
    assert {key: after.get("hooks", {}).get(key) for key in hooks} == hooks
    assert {key: after.get(key) for key in before} == before
    assert catalog.read_bytes() == original_catalog

    sessions = agent_sessions(session, "codex")
    (parent,) = [r for p, r in sessions.items() if not is_child_session("codex", p, r)]
    (child,) = [r for p, r in sessions.items() if is_child_session("codex", p, r)]
    parent_meta = next(r["payload"] for r in parent if r.get("type") == "session_meta")
    child_meta = next(r["payload"] for r in child if r.get("type") == "session_meta")
    assert child_meta["source"]["subagent"]["thread_spawn"]["parent_thread_id"] == parent_meta["id"]
    managed = tomllib.loads(Path("/etc/codex/managed_config.toml").read_text())
    assert managed["model_provider"] == "Databricks"
    assert parent_meta["model_provider"].startswith("Databricks-ug-")
    assert child_meta["model_provider"] == parent_meta["model_provider"]

    (decision,) = read_jsonl(session.home / ".ucode/codex-smart-routing-decisions.jsonl")
    assert decision["session_id"] == parent_meta["id"]
    assert decision["task_name"] == assignment
    calls = [
        r["payload"]
        for r in parent
        if r.get("type") == "response_item" and r["payload"].get("type") == "function_call"
    ]
    assert all(call.get("namespace") == "collaboration" for call in calls)
    (spawn,) = [call for call in calls if call.get("name") == "spawn_agent"]
    assert spawn["encrypted_function_args"] == []
    arguments = json.loads(spawn["arguments"])
    assert arguments["message"] == assignment
    assert arguments["task_name"] == task_name
    (hook,) = read_jsonl(session.home / ".codex/native-pre-tool-use.jsonl")
    assert hook["session_id"] == parent_meta["id"]
    assert hook["tool_use_id"] == spawn["call_id"]
    assert hook["tool_input"]["message"] == assignment

    parent_contexts = [r["payload"] for r in parent if r.get("type") == "turn_context"]
    parent_turns = {r["turn_id"] for r in parent_contexts}
    child_contexts = [
        r["payload"]
        for r in child
        if r.get("type") == "turn_context" and r["payload"]["turn_id"] not in parent_turns
    ]
    assert parent_contexts and child_contexts
    assert all(r.get("multi_agent_version") == "v2" for r in parent_contexts + child_contexts)
    assert codex.completed_task_models(parent, task.value) == {CODEX_NATIVE_START_MODEL}
    assert_subagent_routed(session, "codex", task)
    session.record("codex-native-v2-pre-tool-use.json", [hook])
