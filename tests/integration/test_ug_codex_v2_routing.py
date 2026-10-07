"""CUJ for native Codex v2 subagent routing through the installed ``ug`` CLI.

This journey intentionally uses the public configure command and Codex's native
collaboration tools.  It does not install an MCP server or exercise a plaintext
MCP routing overlay.
"""

from __future__ import annotations

import json
import tomllib
import uuid
from pathlib import Path

import pytest
from utils.evidence import agent_sessions, is_child_session, read_jsonl
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.codex]

CODEX_NATIVE_START_MODEL = "gpt-5.6-sol"
CODEX_NATIVE_CATALOG_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


class PlainAssignment:
    """A delegated file task whose value is not present in the parent prompt."""

    def __init__(self, session):
        self.filename = f"native-v2-{uuid.uuid4().hex[:12]}.txt"
        self.value = f"native-v2-value:{uuid.uuid4().hex}:!?;"
        (session.cwd / self.filename).write_text(self.value)
        self.task_name = f"ug_v2_plain_assignment_{uuid.uuid4().hex[:12]}"
        self.assignment = (
            f"Read {self.filename} using the native file tool and reply with exactly its contents.\n"
            "Preserve every punctuation mark (!?;:), every line break, and the capitalization; "
            "do not add any label or code fence."
        )
        self.prompt = (
            "Use the native collaboration spawn_agent tool exactly once. "
            f'Use task_name "{self.task_name}" and make the tool message exactly the assignment '
            "between BEGIN ASSIGNMENT and END ASSIGNMENT, preserving every newline, punctuation "
            "mark, and line break. Do not prefix or suffix the tool message.\n"
            "BEGIN ASSIGNMENT\n"
            f"{self.assignment}\n"
            "END ASSIGNMENT\n"
            "Do not read the file yourself. Wait for that one child to complete, then reply with "
            "only the child's returned file value. Do not delegate further."
        )


def _session_metadata(records: list[dict]) -> dict:
    rows = [row.get("payload") for row in records if row.get("type") == "session_meta"]
    # A forked v2 child writes its own metadata first, followed by copied parent
    # history. Later session_meta rows are not another locally spawned child.
    assert rows, "Native transcript has no session metadata"
    metadata = rows[0]
    assert isinstance(metadata, dict), metadata
    return metadata


def _task_completions(records: list[dict]) -> list[tuple[str, str]]:
    completions = []
    for row in records:
        if row.get("type") != "event_msg":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "task_complete":
            continue
        turn_id = payload.get("turn_id")
        answer = payload.get("last_agent_message")
        assert isinstance(turn_id, str) and isinstance(answer, str), payload
        completions.append((turn_id, answer))
    return completions


def _turn_context(records: list[dict], turn_id: str) -> dict:
    contexts = [
        row.get("payload")
        for row in records
        if row.get("type") == "turn_context"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("turn_id") == turn_id
    ]
    assert len(contexts) == 1, contexts
    context = contexts[0]
    assert isinstance(context, dict), context
    return context


def _function_calls(records: list[dict]) -> list[dict]:
    return [
        row["payload"]
        for row in records
        if row.get("type") == "response_item"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == "function_call"
    ]


def _spawn_call(records: list[dict]) -> dict:
    calls = [row for row in _function_calls(records) if row.get("name") == "spawn_agent"]
    populated = [row for row in calls if isinstance(row.get("arguments"), str) and row["arguments"]]
    assert len(populated) == 1, calls
    assert {row.get("call_id") for row in populated} == {populated[0].get("call_id")}
    return populated[0]


def _child_parent_id(records: list[dict]) -> str | None:
    metadata = _session_metadata(records)
    source = metadata.get("source")
    if not isinstance(source, dict):
        return None
    thread_spawn = source.get("subagent", {}).get("thread_spawn", {})
    return thread_spawn.get("parent_thread_id")


def _seed_user_owned_native_codex_settings(session) -> tuple[Path, bytes, bytes]:
    """Seed a real Codex native catalog and retain its exact user-owned bytes."""
    result = session.run(
        "debug",
        "models",
        "--bundled",
        binary="codex",
        timeout=60,
    )
    bundled = json.loads(result.stdout)
    native_models = bundled.get("models") if isinstance(bundled, dict) else None
    assert isinstance(native_models, list) and all(
        isinstance(model, dict) for model in native_models
    )
    by_slug = {model.get("slug"): model for model in native_models}
    assert all(slug in by_slug for slug in CODEX_NATIVE_CATALOG_MODELS), {
        "required": CODEX_NATIVE_CATALOG_MODELS,
        "bundled": sorted(slug for slug in by_slug if isinstance(slug, str)),
    }
    selected = [by_slug[slug] for slug in CODEX_NATIVE_CATALOG_MODELS]

    codex_home = session.home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    catalog_path = codex_home / "native-user-model-catalog.json"
    catalog_path.write_text(json.dumps({"models": selected}, indent=2) + "\n")
    config_path = codex_home / "config.toml"
    config_path.write_text(
        "# User-owned native Codex settings for the routing CUJ.\n"
        f"model = {json.dumps(CODEX_NATIVE_START_MODEL)}\n"
        f"model_catalog_json = {json.dumps(str(catalog_path))}\n"
        'personality = "friendly"\n'
    )

    config = tomllib.loads(config_path.read_text())
    assert config["model"] == CODEX_NATIVE_START_MODEL, config
    assert config["model_catalog_json"] == str(catalog_path), config
    config_bytes = config_path.read_bytes()
    catalog_bytes = catalog_path.read_bytes()
    assert json.loads(catalog_bytes)["models"] == selected
    session.record(
        "codex-native-user-settings.json",
        {
            "config_path": str(config_path),
            "catalog_path": str(catalog_path),
            "start_model": CODEX_NATIVE_START_MODEL,
            "catalog_models": CODEX_NATIVE_CATALOG_MODELS,
            "catalog_source": "codex debug models --bundled",
        },
    )
    return catalog_path, config_bytes, catalog_bytes


def test_ug_codex_native_v2_plain_assignment_routing(live_session, workspace):
    """Scenario: configure the installed ``ug`` publicly, launch native Codex v2 with
    subagent-only Smart Router enabled, and delegate one multiline file assignment through
    Codex's native collaboration ``spawn_agent`` function.

    Expected: a native Codex catalog obtained from the installed binary supplies the known
    ``gpt-5.6-sol``, ``gpt-5.6-terra``, and ``gpt-5.6-luna`` slugs while the parent config starts
    on ``gpt-5.6-sol``. The real gateway records one decision whose task is the exact plaintext
    message sent in the native function call; the native child is depth-one, reads the
    unpredictable file value, completes on the model requested by that decision, and the parent
    relays that value. The user-owned model, catalog, and preferences survive configure and
    launch; native TUI trust choices may add their own records.
    No MCP tool, plaintext-routing MCP overlay, or grandchild participates. The non-normal
    workspace catalog limitation is recorded here: the route-supported bundled slugs are seeded
    from the local Codex binary because the ordinary workspace catalog does not provide this
    native test catalog.
    """
    session = live_session
    task = PlainAssignment(session)
    catalog_path, user_config_before, user_catalog_before = _seed_user_owned_native_codex_settings(
        session
    )

    # Keep this CUJ on the native v2 hook path.  The runner's clean environment must not carry
    # the rejected plaintext-MCP prototype switch into the installed session.
    session.env["ENABLE_SMART_ROUTING_V2"] = "1"
    session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    assert "UCODE_CODEX_PLAINTEXT_ROUTING" not in session.env

    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    user_config_path = session.home / ".codex" / "config.toml"
    assert user_config_path.read_bytes() == user_config_before
    assert catalog_path.read_bytes() == user_catalog_before
    native_models = session.codex_model_ids(
        ["app-server", "--listen", "stdio://"],
        name="codex-native-user-catalog",
        binary="codex",
    )
    assert native_models == CODEX_NATIVE_CATALOG_MODELS, native_models

    with AgentTerminal(
        session,
        "codex",
        [str(session.binary), "codex"],
        "codex-native-v2-routing",
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)

        def parent_relays_file_value(_screen: str) -> bool:
            parent_answers = [
                answer
                for path, records in agent_sessions(session, "codex").items()
                if not is_child_session("codex", path, records)
                for _, answer in _task_completions(records)
            ]
            if any(task.value in answer for answer in parent_answers):
                return True
            if parent_answers:
                raise AssertionError(
                    "Parent completed without relaying the unpredictable file value: "
                    + repr(parent_answers)
                )
            return False

        tui.wait_for(
            parent_relays_file_value,
            "the parent to relay the child file value",
            timeout=300,
        )
        tui.exit_normally()

    # Native onboarding records the project/hook trust choices made through its
    # UI. Preserve the user's settings, rather than forbidding those native writes.
    original_config = tomllib.loads(user_config_before.decode())
    after_config = tomllib.loads(user_config_path.read_text())
    assert {key: after_config.get(key) for key in original_config} == original_config
    assert catalog_path.read_bytes() == user_catalog_before

    decision_rows = read_jsonl(session.home / ".ucode/codex-smart-routing-decisions.jsonl")
    session.record("codex-native-v2-routing-decisions.json", decision_rows)
    sessions = agent_sessions(session, "codex")
    assert sessions, "Codex produced no native session transcript"

    parent_sessions = [
        (path, records)
        for path, records in sessions.items()
        if not is_child_session("codex", path, records)
    ]
    child_sessions = [
        (path, records)
        for path, records in sessions.items()
        if is_child_session("codex", path, records)
    ]
    assert len(parent_sessions) == 1, [path for path, _ in parent_sessions]
    assert len(child_sessions) == 1, [path for path, _ in child_sessions]
    parent_path, parent_records = parent_sessions[0]
    child_path, child_records = child_sessions[0]
    parent_metadata = _session_metadata(parent_records)
    child_metadata = _session_metadata(child_records)
    parent_id = parent_metadata.get("id")
    child_id = child_metadata.get("id")
    assert isinstance(parent_id, str) and isinstance(child_id, str)
    assert _child_parent_id(child_records) == parent_id
    thread_spawn = child_metadata["source"]["subagent"]["thread_spawn"]
    assert thread_spawn.get("depth") == 1, thread_spawn

    assert len(decision_rows) == 1, decision_rows
    decision = decision_rows[0]
    assert decision["session_id"] == parent_id, decision
    assert decision["task_name"] == task.assignment, decision
    requested_model = decision.get("requested_model")
    assert isinstance(requested_model, str) and requested_model, decision

    call = _spawn_call(parent_records)
    assert call.get("namespace") == "collaboration", call
    # The native v2 adapter maps the plaintext call back to Codex's native namespace and keeps
    # the optional encrypted argument list empty; the parent-side model field is intentionally not
    # used as proof because the PreToolUse hook may not rewrite the persisted function-call item.
    assert call.get("encrypted_function_args") == [], call
    call_arguments = json.loads(call["arguments"])
    assert call_arguments["task_name"] == task.task_name, call_arguments
    assert call_arguments["message"] == task.assignment, call_arguments
    assert call_arguments["message"] == decision["task_name"], (call_arguments, decision)

    # Native collaboration is the only delegation surface in this CUJ.  Reject MCP and the
    # removed plaintext route-child prototype even if a future Codex transcript emits another
    # native function call while waiting for the child.
    parent_calls = _function_calls(parent_records)
    parent_call_ids = {call.get("call_id") for call in parent_calls}
    child_calls = [
        call
        for call in _function_calls(child_records)
        if call.get("call_id") not in parent_call_ids
    ]
    calls = parent_calls + child_calls
    assert all(
        call.get("namespace") == "collaboration" for call in _function_calls(parent_records)
    ), (
        "The parent used a non-native delegation namespace",
        _function_calls(parent_records),
    )
    assert not any("mcp" in str(call.get("namespace", "")).lower() for call in calls), calls
    assert not any(
        call.get("name") in {"route_child", "plaintext_route_child"} for call in calls
    ), calls
    assert not any(call.get("name") == "spawn_agent" for call in child_calls), (
        "The child attempted to delegate a grandchild",
        child_calls,
    )

    child_completions = [
        (turn_id, answer)
        for turn_id, answer in _task_completions(child_records)
        if task.value in answer
    ]
    parent_completions = [
        (turn_id, answer)
        for turn_id, answer in _task_completions(parent_records)
        if task.value in answer
    ]
    assert len(child_completions) == 1, child_completions
    assert len(parent_completions) == 1, parent_completions
    child_turn_id, _ = child_completions[0]
    parent_turn_id, _ = parent_completions[0]
    child_context = _turn_context(child_records, child_turn_id)
    parent_context = _turn_context(parent_records, parent_turn_id)
    assert child_context.get("multi_agent_version") == "v2", child_context
    assert parent_context.get("multi_agent_version") == "v2", parent_context
    assert child_context.get("model") == requested_model, {
        "child_context": child_context,
        "decision": decision,
    }

    grandchildren = [
        (path, records)
        for path, records in sessions.items()
        if is_child_session("codex", path, records) and _child_parent_id(records) == child_id
    ]
    assert not grandchildren, [path for path, _ in grandchildren]

    audit_rows = read_jsonl(session.home / ".ucode/codex-smart-routing-audit.jsonl")
    matching_audit = [
        row for row in audit_rows if row.get("decision_id") == decision["decision_id"]
    ]
    # Codex 0.154 may expose the selected model only in the child transcript; when it emits the
    # optional SubagentStart audit too, require that independent hook evidence to agree.
    assert len(matching_audit) <= 1, audit_rows
    if matching_audit:
        audit = matching_audit[0]
        assert audit.get("matches_router_decision") is True, audit
        assert audit.get("model") == requested_model, audit
    else:
        audit = None
    if "profiler_status" in decision:
        assert decision["profiler_status"] == "ok", decision

    session.record(
        "codex-native-v2-routing-evidence.json",
        {
            "parent_path": parent_path,
            "child_path": child_path,
            "decision": decision,
            "native_spawn_call": {
                "namespace": call.get("namespace"),
                "name": call.get("name"),
                "call_id": call.get("call_id"),
                "arguments": call_arguments,
                "encrypted_function_args": call.get("encrypted_function_args"),
            },
            "child_turn_id": child_turn_id,
            "child_model": requested_model,
            "audit": audit,
            "grandchildren": [],
        },
    )
