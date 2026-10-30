"""Native lifecycle verification for the direct Responses transport candidate."""

from __future__ import annotations

import hashlib
import tomllib

import pytest
from test_ug_codex_v2_routing import _seed_user_owned_native_codex_settings
from test_ug_smart_routing_hooks import _toggle_with_skill
from utils.evidence import (
    FileTask,
    agent_sessions,
    assert_no_terminal_api_error,
    assert_subagent_routed,
    is_child_session,
    read_jsonl,
)
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.codex]


def _metadata(records):
    return next(row["payload"] for row in records if row.get("type") == "session_meta")


def _completed_models(records, value):
    turns = {
        row["payload"]["turn_id"]
        for row in records
        if row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "task_complete"
        and value in (row["payload"].get("last_agent_message") or "")
    }
    contexts = {
        row["payload"]["turn_id"]: row["payload"]["model"]
        for row in records
        if row.get("type") == "turn_context"
    }
    assert turns and turns <= contexts.keys()
    return {contexts[turn] for turn in turns}


def _assert_preserved(before, after):
    if isinstance(before, dict):
        assert isinstance(after, dict)
        for key, value in before.items():
            assert key in after
            _assert_preserved(value, after[key])
    else:
        assert before == after


def test_ug_codex_native_v2_comprehensive_lifecycle(live_session, workspace, tmp_path):
    """Scenario: configure a real installed Codex with code-mode v2 collaboration,
    route children, follow up, compact, resume, and toggle routing off and on with
    user-configured on-request approvals.

    Expected: native hooks and children receive exact plaintext, completed child
    models match real routing decisions, the parent remains pinned, the original
    child survives compaction/resume, user settings survive, and both TUIs exit
    normally. This case uses the real gateway without a traffic recorder.
    """
    session = live_session
    session.env.update(
        ENABLE_SMART_ROUTING_V2="1",
        ENABLE_SMART_ROUTING_SUBAGENT_ONLY="1",
        TMPDIR=str(tmp_path),
    )
    catalog, original, catalog_bytes = _seed_user_owned_native_codex_settings(session)
    config = session.home / ".codex/config.toml"
    # Windows protects the private control directory; exercise the user's approved toggle.
    original = b'approval_policy = "on-request"\n' + original
    config.write_bytes(original)
    version = session.run("--version", binary=session.which("codex")).stdout.strip()
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
    assert config.read_bytes() == original
    summary = {"version": version, "phases": [], "passed": False}
    parent_id = None
    routed = True

    def sessions():
        return agent_sessions(session, "codex")

    def parent():
        candidates = [
            records
            for path, records in sessions().items()
            if any(row.get("type") == "session_meta" for row in records)
            and not is_child_session("codex", path, records)
            and (parent_id is None or _metadata(records)["id"] == parent_id)
        ]
        assert len(candidates) == 1
        return candidates[0]

    def ordinary(tui, name):
        task = FileTask(session)
        tui.submit(task.prompt + " Do this directly without delegation.")
        tui.wait_for_task(task, timeout=180)
        assert _completed_models(parent(), task.value) == {"gpt-5.6-sol"}
        summary["phases"].append({"name": name, "value": task.value, "passed": True})
        return task

    def delegation(tui, name, target=None):
        task = FileTask(session)
        assignment = task.prompt + '\nKeep this literal: café λ; "quotes", \\slashes, {braces}.'
        tool = "followup_task" if target else "spawn_agent"
        selector = f"target {target!r}" if target else f"task_name {name!r}"
        tui.submit(
            f"Use native {tool} with {selector} and exactly the message between BEGIN and END. "
            "Omit model overrides. Do not read the file yourself. Wait for the child and return "
            "only its file value.\n"
            f"BEGIN\n{assignment}\nEND"
        )
        tui.wait_for_task(task, timeout=300)
        task.assert_completed(session, "codex", child=True)
        matching = [
            records
            for path, records in sessions().items()
            if is_child_session("codex", path, records)
            and any(
                content.get("text", "").endswith(assignment)
                for row in records
                if row.get("type") == "response_item"
                and row.get("payload", {}).get("type") == "agent_message"
                for content in row["payload"].get("content", [])
                if content.get("type") == "input_text"
            )
        ]
        assert len(matching) == 1
        child = matching[0]
        assert _completed_models(parent(), task.value) == {"gpt-5.6-sol"}
        child_models = _completed_models(child, task.value)
        if target is None:
            hooks = [
                row
                for row in read_jsonl(session.home / ".codex/native-pre-tool-use.jsonl")
                if row.get("tool_input", {}).get("message") == assignment
            ]
            assert len(hooks) == 1
            decisions = [
                row
                for row in read_jsonl(session.home / ".ucode/codex-smart-routing-decisions.jsonl")
                if row.get("task_name") == assignment
            ]
            if routed:
                assert len(decisions) == 1
                assert_subagent_routed(
                    session, "codex", task, decision_ids={decisions[0]["decision_id"]}
                )
                assert child_models == {decisions[0]["requested_model"]}
            else:
                assert decisions == []
                assert child_models == {"gpt-5.6-sol"}
        phase = {
            "name": name,
            "child_id": _metadata(child)["id"],
            "value": task.value,
            "assignment": assignment,
            "child_models": sorted(child_models),
            "passed": True,
        }
        summary["phases"].append(phase)
        session.record("native-v2-summary.json", summary)
        return phase

    command = [str(session.binary), "codex"]
    try:
        with AgentTerminal(session, "codex", command, "native-v2-lifecycle") as tui:
            tui.boot()
            ordinary(tui, "ordinary_before")
            parent_id = _metadata(parent())["id"]
            first = delegation(tui, "first_child")
            assert delegation(tui, "followup", "first_child")["child_id"] == first["child_id"]
            tui.submit("/compact")

            def compacted(screen):
                assert_no_terminal_api_error(screen)
                records = parent()
                completed = {
                    row["payload"]["turn_id"]
                    for row in records
                    if row.get("type") == "event_msg"
                    and row.get("payload", {}).get("type") == "task_complete"
                }
                return any(row.get("type") == "compacted" for row in records) and any(
                    row.get("type") == "event_msg"
                    and row.get("payload", {}).get("type") == "item_completed"
                    and row["payload"].get("item", {}).get("type") == "ContextCompaction"
                    and row["payload"].get("turn_id") in completed
                    for row in records
                )

            tui.wait_for(compacted, "native compaction completes", timeout=240)
            delegation(tui, "after_compaction")
            previous = delegation(tui, "old_child_after_compaction", "first_child")
            assert previous["child_id"] == first["child_id"]
            tui.exit_normally()

        # Each launch has its own controls; the skill helper must inspect this launch's file.
        resume_tmp = tmp_path / "r"
        resume_tmp.mkdir()
        session.env["TMPDIR"] = str(resume_tmp)
        with AgentTerminal(session, "codex", command, "native-v2-resume") as tui:
            tui.boot()
            tui.submit("/resume " + parent_id)
            tui.wait_for(
                lambda screen: previous["value"] in screen, "prior answer after /resume", timeout=90
            )
            ordinary(tui, "ordinary_after_resume")
            delegation(tui, "new_child_after_resume")
            assert (
                delegation(tui, "old_child_after_resume", "first_child")["child_id"]
                == first["child_id"]
            )
            for enabled in [False, True]:
                _toggle_with_skill(
                    tui, session, "codex", enabled=enabled, orchestration_enabled=False
                )
                routed = enabled
                delegation(tui, "routing_on" if enabled else "routing_off")
            tui.exit_normally()

        _assert_preserved(tomllib.loads(original.decode()), tomllib.loads(config.read_text()))
        assert catalog.read_bytes() == catalog_bytes
        all_sessions = sessions()
        assert all(
            _metadata(records)["model_provider"] == "Databricks"
            for records in all_sessions.values()
        )
        versions = {
            row["payload"].get("multi_agent_version")
            for records in all_sessions.values()
            for row in records
            if row.get("type") == "turn_context"
        }
        assert versions == {"v2"}
        assert all(
            row["payload"].get("approval_policy") == "on-request"
            for records in all_sessions.values()
            for row in records
            if row.get("type") == "turn_context"
        )
        assert session.run("--version", binary=session.which("codex")).stdout.strip() == version
        summary.update(
            passed=True,
            parent_id=parent_id,
            native_versions=sorted(versions),
            user_config_preserved=True,
            normal_exits=True,
        )
    finally:
        session.record("native-v2-summary.json", summary)
        session.record("native-v2-sessions.json", sessions())
        session.record(
            "native-v2-hooks.json", read_jsonl(session.home / ".codex/native-pre-tool-use.jsonl")
        )
        session.record(
            "native-v2-decisions.json",
            read_jsonl(session.home / ".ucode/codex-smart-routing-decisions.jsonl"),
        )
        session.record(
            "native-v2-settings.json",
            {
                "before": original.decode(),
                "after": config.read_text(),
                "catalog_before_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
                "catalog_after_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
            },
        )
