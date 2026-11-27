"""Native Codex v2 assignments must reach the routing hook as plaintext."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tomllib
from collections import Counter
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
from utils.managed import use_managed_config_fixture
from utils.request_recorder import TuiRequestRecorder
from utils.terminal import AgentTerminal

pytestmark = [
    pytest.mark.tui,
    pytest.mark.codex,
    pytest.mark.codex_smart_routing,
]

CODEX_NATIVE_START_MODEL = "gpt-5.6-terra"
CODEX_NATIVE_CATALOG_MODELS = ["gpt-5.6-terra", "gpt-6-luna"]


def _seed_user_owned_native_codex_settings(session) -> tuple[Path, bytes, bytes]:
    # The workspace catalog includes older models; this user-owned catalog forces v2.
    result = session.run("debug", "models", "--bundled", binary=session.which("codex"), timeout=60)
    models = json.loads(result.stdout)["models"]
    selected = [model for model in models if model["slug"] in CODEX_NATIVE_CATALOG_MODELS]
    assert {model["slug"] for model in selected} == set(CODEX_NATIVE_CATALOG_MODELS)
    assert all(model.get("multi_agent_version") == "v2" for model in selected)
    for model in selected:
        model["multi_agent_tools_in_code_mode"] = False

    codex_home = session.home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    catalog = codex_home / "native-user-model-catalog.json"
    catalog.write_text(json.dumps({"models": selected}))

    # Observe actual native stdin without modifying the hook input or result.
    observer = codex_home / "record-pre-tool-use.py"
    hook_input = codex_home / "native-pre-tool-use.jsonl"
    observer.write_text(
        "import json, sys\n"
        f"with open({str(hook_input)!r}, 'a', encoding='utf-8') as output:\n"
        "    output.write(json.dumps(json.load(sys.stdin.buffer)) + '\\n')\n"
        "print('{}')\n"
    )
    config = codex_home / "config.toml"
    command = [sys.executable, str(observer)]
    command_line = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    config.write_text(
        f"model = {json.dumps(CODEX_NATIVE_START_MODEL)}\n"
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
        "[[hooks.PreToolUse]]\n"
        'matcher = "Agent|.*spawn_agent$"\n'
        "[[hooks.PreToolUse.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(command_line)}\n"
    )
    return catalog, config.read_bytes(), catalog.read_bytes()


@pytest.mark.parametrize(
    ("model_args", "managed_catalog"),
    [
        pytest.param((), False, id="saved", marks=pytest.mark.live),
        pytest.param(
            ("--model", CODEX_NATIVE_CATALOG_MODELS[1]), False, id="split", marks=pytest.mark.live
        ),
        pytest.param(
            ("--model=system.ai.gpt-6-luna",),
            True,
            id="managed-equals",
            marks=pytest.mark.managed_fixture,
        ),
    ],
)
def test_ug_codex_native_v2_plain_assignment_routing(
    live_session, workspace, model_args, managed_catalog
):
    """Scenario: configure ug with a user-owned native v2 catalog that disables tools
    in code mode and an observer hook, then delegate one multiline file task with
    subagent-only routing. The managed variant configures UG's static catalog through
    the normal managed-fixture input. UG must expose the plaintext tool path. Launch with
    the saved model, split `--model VALUE`, or managed equals-form `--model=VALUE` syntax.

    Expected: every routing request has a paired HTTP 200 response and matching decision.
    The request for the complete assignment selects the model used by completed native
    child work. The parent stays on its selected model, overriding the saved model when
    explicitly supplied. Requests use the configured Databricks provider through the recorder and
    preserve the user's config, without user-supplied feature flags. The observer sees
    the original task name; routed children carry the enabled attribution suffix.
    """
    session = live_session
    task = FileTask(session)
    assignment = task.prompt + '\nKeep this literal: café λ; "quotes", \\slashes, {braces}.'
    task_name = "ug_native_file"
    prompt = (
        f'Delegate to one native child with task_name "{task_name}". '
        "Set message to exactly the assignment between BEGIN and END, preserving the newline.\n"
        f"BEGIN\n{assignment}\nEND\n"
        "Do not read the file yourself. Wait for the child and return only its file value."
    )
    catalog, original_config, original_catalog = _seed_user_owned_native_codex_settings(session)
    if managed_catalog:
        use_managed_config_fixture(session, "codex_lifecycle_a")
    session.env["ENABLE_SMART_ROUTING_V2"] = "1"
    session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    with TuiRequestRecorder(workspace) as recorder:
        session.run(
            "configure",
            "--agents",
            "codex",
            "--workspace",
            recorder.url,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )

        config = session.home / ".codex/config.toml"
        assert config.read_bytes() == original_config
        start_model = CODEX_NATIVE_START_MODEL
        provider_config = (
            session.home / ".codex/ucode.config.toml"
            if os.name == "nt"
            else Path("/etc/codex/managed_config.toml")
        )
        configured_catalog = None
        if managed_catalog:
            settings = tomllib.loads(provider_config.read_text())
            configured_catalog = Path(settings["model_catalog_json"])
            managed_before = configured_catalog.read_bytes()
            models = json.loads(managed_before)["models"]
            assert [m["slug"] for m in models if m["visibility"] == "list"] == [
                "system.ai.gpt-5-6-terra",
                "system.ai.gpt-6-luna",
            ]
            assert all(m.get("multi_agent_tools_in_code_mode") is not False for m in models)
            start_model = "system.ai.gpt-5-6-terra"
        launch_args = []
        if model_args:
            start_model = model_args[-1].removeprefix("--model=")
            launch_args = ["--enable-smart-routing", "--", *model_args]
        session.record("launch-model.json", {"model": start_model, "args": launch_args})
        request_checkpoint = recorder.checkpoint()
        with AgentTerminal(
            session,
            "codex",
            [str(session.binary), "codex", *launch_args],
            "codex-native-v2-routing",
        ) as tui:
            tui.boot()
            tui.submit(prompt)
            tui.wait_for_task(task, timeout=300)
            tui.exit_normally()

        hooks = read_jsonl(session.home / ".codex/native-pre-tool-use.jsonl")
        assignments = {
            row["tool_input"]["message"]
            for row in hooks
            if row.get("tool_input", {}).get("task_name") == task_name
            and isinstance(row["tool_input"].get("message"), str)
        }
        assert len(assignments) == 1, assignments
        native_assignment = assignments.pop()
        # The parent may include the newline immediately before the END delimiter.
        assert native_assignment in (assignment, assignment + "\n"), native_assignment
        assignment = native_assignment

        routing_requests = [
            request
            for request in recorder.requests_after(request_checkpoint)
            if request.method == "POST" and request.path == "/ai-gateway/routing/v1/routes:select"
        ]
        assert routing_requests, "Codex made no live routes:select request"
        routing_outcomes = Counter()
        routing_prompts = []
        routing_receipts = []
        for request in routing_requests:
            route_prompt = request.payload["task"]["prompt"]
            assert isinstance(route_prompt, str), request.sequence
            response = recorder.response_for(request, timeout=240)
            assert response.status_code == 200, (request.sequence, response.status_code)
            selections = response.payload["route_selection"]
            assert (
                isinstance(selections, list)
                and len(selections) == 1
                and isinstance(selections[0], dict)
                and isinstance(selections[0].get("route_option"), dict)
            ), (request.sequence, response.payload)
            option = selections[0]["route_option"]
            assert option in request.payload["route_options"], request.sequence
            routing_prompts.append(route_prompt)
            routing_outcomes[
                (
                    hashlib.sha256(route_prompt.encode("utf-8")).hexdigest(),
                    len(route_prompt),
                    option["model"],
                )
            ] += 1
            routing_receipts.append(
                {
                    "sequence": request.sequence,
                    "request": request.payload,
                    "status": response.status_code,
                    "response": response.payload,
                }
            )
        session.record("codex-routing-responses.json", routing_receipts)
        assert assignment in routing_prompts, "No route request carried the complete assignment"

    # Native trust records may be added; user settings must remain intact.
    before, after = tomllib.loads(original_config.decode()), tomllib.loads(config.read_text())
    configured_hooks = before.pop("hooks")
    assert {key: after.get("hooks", {}).get(key) for key in configured_hooks} == configured_hooks
    assert {key: after.get(key) for key in before} == before
    assert catalog.read_bytes() == original_catalog
    if configured_catalog is not None:
        assert configured_catalog.read_bytes() == managed_before

    sessions = agent_sessions(session, "codex")
    parent_sessions = []
    child_sessions = []
    for path, records in sessions.items():
        metadata = next(
            (row["payload"] for row in records if row.get("type") == "session_meta"), None
        )
        if metadata is None:
            continue
        if is_child_session("codex", path, records):
            child_sessions.append((records, metadata))
        else:
            parent_sessions.append((records, metadata))

    decisions = read_jsonl(session.home / ".ucode/codex-smart-routing-decisions.jsonl")
    decision_outcomes = Counter(
        (row.get("task_sha256"), row.get("task_length"), row.get("router_model"))
        for row in decisions
    )
    assert routing_outcomes == decision_outcomes, (routing_outcomes, decision_outcomes)
    assignment_digest = hashlib.sha256(assignment.encode("utf-8")).hexdigest()
    assignment_decisions = [
        row
        for row in decisions
        if row.get("task_sha256") == assignment_digest and row.get("task_length") == len(assignment)
    ]
    assert assignment_decisions, "No routing decision matched the complete assignment"
    decision_ids = {row["decision_id"] for row in assignment_decisions}
    decision_parent_ids = {row["session_id"] for row in assignment_decisions}
    assert all("task_name" not in row for row in assignment_decisions)

    matching_parents = [
        (records, metadata)
        for records, metadata in parent_sessions
        if metadata["id"] in decision_parent_ids
    ]
    assert {metadata["id"] for _, metadata in matching_parents} == decision_parent_ids
    assert child_sessions
    parent_provider = {metadata["model_provider"] for _, metadata in matching_parents}
    child_provider = {metadata["model_provider"] for _, metadata in child_sessions}
    assert parent_provider == {"Databricks"}
    assert child_provider == parent_provider
    assert tomllib.loads(provider_config.read_text())["model_provider"] == "Databricks"

    matching_hooks = [
        row
        for row in hooks
        if row.get("session_id") in decision_parent_ids
        and row.get("tool_input", {}).get("message") == assignment
    ]
    assert matching_hooks, "No native hook carried the complete assignment"
    assert {row["session_id"] for row in matching_hooks} == decision_parent_ids
    for hook in matching_hooks:
        assert hook["tool_input"]["message"] == assignment
        assert hook["tool_input"]["task_name"] == task_name
    session.record("codex-native-v2-pre-tool-use.json", matching_hooks)

    parent_contexts = [
        row["payload"]
        for records, _ in matching_parents
        for row in records
        if row.get("type") == "turn_context"
    ]
    parent_turns = {row["turn_id"] for row in parent_contexts}
    child_contexts = [
        row["payload"]
        for records, _ in child_sessions
        for row in records
        if row.get("type") == "turn_context" and row["payload"]["turn_id"] not in parent_turns
    ]
    assert parent_contexts and child_contexts
    assert all(r.get("multi_agent_version") == "v2" for r in parent_contexts + child_contexts)
    parent_models = [
        (metadata["id"], codex.completed_task_models(records, task.value))
        for records, metadata in matching_parents
    ]
    assert parent_models and all(models == {start_model} for _, models in parent_models), (
        parent_models
    )

    child_paths = []
    child_ids = set()
    for records, metadata in child_sessions:
        child_path = metadata["source"]["subagent"]["thread_spawn"]["agent_path"]
        received_assignment = any(
            content.get("text", "").endswith(assignment)
            for row in records
            if row.get("type") == "response_item"
            and row.get("payload", {}).get("type") == "agent_message"
            and row["payload"].get("recipient") == child_path
            for content in row["payload"].get("content", [])
            if content.get("type") == "input_text"
        )
        if not received_assignment:
            continue
        task_complete = any(
            row.get("type") == "event_msg"
            and row["payload"].get("type") == "task_complete"
            and row["payload"].get("turn_id") not in parent_turns
            and task.value in (row["payload"].get("last_agent_message") or "")
            for row in records
        )
        assert task_complete, child_path
        child_paths.append(child_path)
        child_ids.add(metadata["id"])
    assert child_paths, "No native child received the complete assignment"
    assert all(path == f"/root/{task_name}__sr_on" for path in child_paths)
    assert_subagent_routed(session, "codex", task, decision_ids=decision_ids, child_ids=child_ids)
