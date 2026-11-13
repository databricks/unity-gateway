"""Live native-agent continuity across Smart Router metadata changes."""

import json
import os

import pytest
from test_ug_smart_routing_hooks import _select_smart_router_version, _toggle_with_skill
from utils.agents.codex import assert_agent_interaction_completed, completed_command_outputs
from utils.evidence import (
    agent_sessions,
    assistant_answers,
    fresh_id,
    is_child_session,
    tool_outputs,
)
from utils.terminal import AgentTerminal


def _native_agents(session):
    agents = {}
    for records in agent_sessions(session, "codex").values():
        for row in records:
            if row.get("type") != "session_meta":
                continue
            metadata = row["payload"]
            source = metadata.get("source")
            if isinstance(source, dict):
                spawn = source.get("subagent", {}).get("thread_spawn", {})
                if spawn.get("agent_path"):
                    agents[spawn["agent_path"]] = (metadata["id"], records)
    return agents


def _root_sessions(session):
    return (
        records
        for path, records in agent_sessions(session, "codex").items()
        if not is_child_session("codex", path, records)
    )


def _root_answers(session):
    return [
        answer
        for records in _root_sessions(session)
        for answer in assistant_answers("codex", records)
    ]


def _root_user_prompts(session):
    prompts = []
    for records in _root_sessions(session):
        for row in records:
            payload = row.get("payload", {})
            if row.get("type") != "response_item" or payload.get("role") != "user":
                continue
            text = " ".join(item.get("text", "") for item in payload.get("content", []))
            if text and not text.startswith(("<environment_context>", "<skill>")):
                prompts.append(text)
    return prompts


def _assert_main_turn_states(session, expected):
    root_ids = {
        row["payload"]["id"]
        for records in _root_sessions(session)
        for row in records
        if row.get("type") == "session_meta"
    }
    assert len(root_ids) == 1, root_ids
    (root_id,) = root_ids
    log = session.routing_log("codex")
    states = [
        line.split("state=", 1)[1].split()[0]
        for line in log.splitlines()
        if "[METADATA] main turn:" in line and line.endswith(f"thread={root_id}")
    ]
    assert states == expected, log


def _wait_for(tui, predicate, description, *, timeout):
    def completed(screen):
        if tui.approve_windows_command(screen):
            return False
        return predicate()

    tui.wait_for(completed, description, timeout=timeout)


def _assert_toggle_executed_once(session, enabled):
    flag = "--enable-smart-routing" if enabled else "--disable-smart-routing"
    outputs = [
        output
        for records in _root_sessions(session)
        for output in completed_command_outputs(
            records, command_contains=f"-m ucode.cli codex {flag}"
        )
    ]
    assert len(outputs) == 1, outputs
    assert f"Smart Router is {'on' if enabled else 'off'} for this session" in outputs[0]


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
@pytest.mark.codex_smart_routing
def test_codex_routing_toggle_preserves_busy_child_and_nested_registry(
    live_session, workspace, tmp_path
):
    """Scenario: launch with the native workspace-write sandbox, keep a child running,
    turn routing off through the installed skill,
    spawn a new sibling, then let the old child spawn a grandchild and message its new sibling.
    The root messages the grandchild and follows up with the original child.

    Expected: one uninterrupted old child retains its ID and on marker. New sibling and
    grandchild have off markers. Messages across both branches succeed, the original child
    resumes, hidden file values reach completed answers, and no conversation is reloaded.
    Native transcripts prove identity and continuity with no interrupted turns or additional user prompts;
    usage-table validation is separate.
    """
    session = live_session
    _select_smart_router_version(session, "codex", workspace, tmp_path, False, "subagent_only_v0")
    session.run(
        "configure", "--workspace", workspace, "--skip-upgrade", "--disable-databricks-ai-tools"
    )
    marker = fresh_id()
    started = session.cwd / f"{marker}-started"
    release = session.cwd / f"{marker}-release"
    finished = session.cwd / f"{marker}-finished"
    value = fresh_id()
    secret = session.cwd / f"{marker}-value"
    secret.write_text(value)
    gate = session.cwd / f"{marker}-gate.py"
    gate.write_text(
        "import os, time\nfrom pathlib import Path\n"
        f"started = Path({str(started)!r})\nrelease = Path({str(release)!r})\n"
        f"finished = Path({str(finished)!r})\n"
        "with started.open('x') as receipt: receipt.write(str(os.getpid()))\n"
        "deadline = time.monotonic() + 420\n"
        "while not release.exists():\n"
        "    if time.monotonic() > deadline: raise TimeoutError('release missing')\n"
        "    time.sleep(0.2)\n"
        "with finished.open('x') as receipt: receipt.write(str(os.getpid()))\n"
    )
    python = (
        '& "$env:UCODE_SMART_ROUTER_PYTHON"' if os.name == "nt" else '"$UCODE_SMART_ROUTER_PYTHON"'
    )
    assignment = (
        f'Run {python} "{gate.name}" exactly once. This command waits for a release file; '
        "let it run without restarting it. After it finishes, spawn exactly one child with "
        f"task_name nested, fork_turns none, asking it to read {secret.name} and return its "
        "entire contents. Then use send_message to send 'old sibling reached new sibling' "
        "to /root/new__sr_off. Wait for your child's answer and return that value."
    )
    with AgentTerminal(
        session,
        "codex",
        [str(session.binary), "codex", "--", "--sandbox=workspace-write"],
        "routing-metadata-busy-child",
    ) as tui:
        tui.boot()
        tui.submit(
            "Spawn exactly one native child with task_name old, fork_turns none, and this "
            f"assignment: {assignment}\nEnd your turn with ONLY 'started {marker}' immediately "
            "after spawning. Do not wait for it or run its commands yourself."
        )
        _wait_for(
            tui,
            lambda: started.is_file() and f"started {marker}" in _root_answers(session),
            "a running child and completed main answer",
            timeout=180,
        )
        old_id, old_records = _native_agents(session)["/root/old__sr_on"]
        assert not finished.exists()
        assert not assistant_answers("codex", old_records)
        _toggle_with_skill(tui, session, "codex", False, False)
        assert not finished.exists(), "Child finished before its release"
        tui.submit(
            "Spawn exactly one new child with task_name new, fork_turns none. Ask it to "
            "reply 'new sibling ready'. Wait for its answer and repeat it. Leave old running."
        )
        _wait_for(
            tui,
            lambda: (
                "new sibling ready" in _root_answers(session)
                and "/root/new__sr_off" in _native_agents(session)
            ),
            "a new off-attributed sibling",
            timeout=180,
        )
        release.write_text("release")
        _wait_for(
            tui,
            lambda: (
                "/root/old__sr_on/nested__sr_off" in _native_agents(session)
                and any(
                    value in answer
                    for answer in assistant_answers(
                        "codex", _native_agents(session)["/root/old__sr_on"][1]
                    )
                )
            ),
            "the original child to complete after spawning an off-attributed grandchild",
            timeout=180,
        )
        receipt = fresh_id()
        tui.submit(
            "Use send_message to /root/old__sr_on/nested__sr_off with message 'root reached "
            "grandchild'. Then use followup_task to /root/old__sr_on asking it to reply exactly "
            f"'{receipt}'. Wait for its answer, then reply with that exact answer. Spawn nothing."
        )
        _wait_for(
            tui,
            lambda: receipt in _root_answers(session),
            "cross-branch messaging and original child follow-up",
            timeout=180,
        )
        tui.exit_normally()
    agents = _native_agents(session)
    assert set(agents) == {
        "/root/old__sr_on",
        "/root/new__sr_off",
        "/root/old__sr_on/nested__sr_off",
    }, agents.keys()
    assert agents["/root/old__sr_on"][0] == old_id
    assert started.read_text() == finished.read_text()
    old_records = agents["/root/old__sr_on"][1]
    outputs = [
        output
        for records in agent_sessions(session, "codex").values()
        for output in tool_outputs("codex", records)
    ]
    assert not any("live agent path" in output and "not found" in output for output in outputs)
    assert_agent_interaction_completed(
        old_records, "/root/new__sr_off", agents["/root/new__sr_off"][0]
    )
    all_records = [row for records in agent_sessions(session, "codex").values() for row in records]
    assert not any(
        row.get("type") == "event_msg" and row["payload"].get("type") == "turn_aborted"
        for row in all_records
    )
    assert_agent_interaction_completed(
        all_records,
        "/root/old__sr_on/nested__sr_off",
        agents["/root/old__sr_on/nested__sr_off"][0],
    )
    assert_agent_interaction_completed(all_records, "/root/old__sr_on", old_id)
    assert sum(receipt in answer for answer in assistant_answers("codex", old_records)) == 1
    assert len(_root_user_prompts(session)) == 4
    _assert_toggle_executed_once(session, False)
    _assert_main_turn_states(session, ["on", "on", "off", "off"])
    session.record(
        "routing-metadata-agents.json", {name: identity for name, (identity, _) in agents.items()}
    )
    session.record("routing-metadata-outputs.json", outputs)


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
@pytest.mark.codex_smart_routing
def test_codex_routing_toggle_without_children_and_repeated_state(
    live_session, workspace, tmp_path
):
    """Scenario: turn routing off and back on without children, repeat on through the public
    session CLI, then complete a task in the same TUI.

    Expected: both transitions complete; repeated on leaves the controls file unchanged,
    causes no interruption, and a subsequent answer completes in the original root.
    Each toggle turn keeps its starting attribution; the next user turn sees the change.
    The native history contains only the three submitted user prompts.
    """
    session = live_session
    _select_smart_router_version(session, "codex", workspace, tmp_path, False, "subagent_only_v0")
    session.run(
        "configure", "--workspace", workspace, "--skip-upgrade", "--disable-databricks-ai-tools"
    )
    marker = fresh_id()
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "routing-metadata-empty-tree"
    ) as tui:
        tui.boot()
        _toggle_with_skill(tui, session, "codex", False, False)
        _toggle_with_skill(tui, session, "codex", True, False)
        (controls,) = tmp_path.glob("ug-session-env-*/env.json")
        before = controls.stat().st_mtime_ns
        session.env["UCODE_SESSION_ENV_FILE"] = str(controls)
        result = session.run("codex", "--enable-smart-routing")
        assert "already on" in result.stdout
        assert controls.stat().st_mtime_ns == before
        tui.submit(f"Reply with only {marker}. Use no tools.")
        tui.wait_for(lambda _: marker in _root_answers(session), "a final main answer", timeout=120)
        tui.exit_normally()
    assert not _native_agents(session)
    assert len(_root_user_prompts(session)) == 3
    _assert_toggle_executed_once(session, False)
    _assert_toggle_executed_once(session, True)
    assert json.loads(controls.read_text()) == {}
    records = [row for root in _root_sessions(session) for row in root]
    assert not any(
        row.get("type") == "event_msg" and row["payload"].get("type") == "turn_aborted"
        for row in records
    )
    _assert_main_turn_states(session, ["on", "off", "on"])
