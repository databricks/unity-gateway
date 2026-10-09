"""Real automatic delegation and continuation after native context compaction."""

import uuid
from pathlib import Path

import pytest
from utils.evidence import (
    agent_sessions,
    assert_no_terminal_api_error,
    completed_answers,
    completed_child_answers,
    is_child_session,
    orchestrator_contexts,
)
from utils.managed import use_managed_config_fixture
from utils.terminal import AgentTerminal


class ReviewTask:
    """Substantive source review whose traceability values are absent from the prompt."""

    def __init__(self, session, phase, sources):
        directory = session.cwd / phase
        directory.mkdir()
        self.phase = phase
        self.values = []
        source_root = Path(__file__).resolve().parents[2] / "src" / "ucode"
        for source in sources:
            value = uuid.uuid4().hex
            self.values.append(value)
            text = f"# REVIEW_ID: {value}\n" + (source_root / source).read_text()
            (directory / Path(source).name).write_text(text)
        self.prompt = (
            f"Review all three Python modules in {phase}/ for correctness, error handling, "
            "and preservation of user data. Read each module and report concrete findings "
            "with file references, or explain why you found none. These are standalone "
            "review copies; dependencies outside this directory are not part of the review. "
            "Do not modify files or execute the source. Include each module's exact "
            "REVIEW_ID from its header in your final report so the review is traceable."
        )

    def completed(self, session, agent):
        return any(
            all(value in answer for value in self.values)
            for answer in completed_answers(agent, _root_records(session, agent))
        )


def _root_records(session, agent):
    return [
        row
        for path, records in agent_sessions(session, agent).items()
        if not is_child_session(agent, path, records)
        for row in records
    ]


def _review(tui, session, agent, task):
    before_children = completed_child_answers(session, agent)
    before_records = len(_root_records(session, agent))
    tui.submit(task.prompt)

    def finished(screen):
        assert_no_terminal_api_error(screen)
        return task.completed(session, agent)

    tui.wait_for(finished, "a completed review of all three modules", timeout=360)
    children = completed_child_answers(session, agent)
    new_answers = {
        path: answers[len(before_children.get(path, [])) :] for path, answers in children.items()
    }
    session.record(
        f"{task.phase}-completion.json",
        {"review_ids": task.values, "new_completed_child_answers": new_answers},
    )
    assert any(answer.strip() for answers in new_answers.values() for answer in answers), (
        "The root completed the review without a completed native child review"
    )
    assert orchestrator_contexts(agent, _root_records(session, agent)[before_records:]), (
        "The native root transcript did not receive a fresh orchestrator workflow"
    )


def _compact(tui, session, agent):
    before = len(_root_records(session, agent))
    tui.submit("/compact")

    def reloaded(screen):
        assert_no_terminal_api_error(screen)
        records = _root_records(session, agent)[before:]
        if agent == "claude":
            compacted = any(row.get("subtype") == "compact_boundary" for row in records)
            contexts = [
                row
                for row in records
                if row.get("attachment", {}).get("hookEvent") == "SessionStart"
            ]
        else:
            compacted = any(
                row.get("type") == "compacted"
                or (
                    row.get("type") == "event_msg"
                    and row.get("payload", {}).get("type") == "context_compacted"
                )
                for row in records
            )
            # Codex runs the compact hook before the next model request.
            # The follow-up review requires its newly delivered workflow.
            return compacted
        return compacted and bool(orchestrator_contexts(agent, contexts))

    tui.wait_for(reloaded, "native compaction", timeout=180)
    session.record("compaction-records.json", _root_records(session, agent)[before:])


@pytest.mark.claude
@pytest.mark.managed_fixture
def test_orchestrator_claude_delegates_and_recovers_after_compaction(
    live_session, workspace, tmp_path
):
    """Scenario: opt into orchestration and ask Claude to review three source modules,
    without requesting subagents; compact natively, then review three different modules.

    Expected: the prompt and compaction hooks deliver the complete workflow; both reviews
    have a completed native child and a root report containing values read from all files.
    This checks delegation and continuation, not the accuracy of the review's findings.
    """
    session = live_session
    session.env.update(
        ENABLE_SMART_ROUTING_V2="1",
        ENABLE_SMART_ROUTING_SUBAGENT_ONLY="1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR="1",
        TMPDIR=str(tmp_path),
    )
    use_managed_config_fixture(session, "claude_smart_routing")
    session.run(
        "configure", "--workspace", workspace, "--skip-upgrade", "--disable-databricks-ai-tools"
    )
    first = ReviewTask(
        session, "initial", ["config_io.py", "gateway_proxy.py", "managed_config.py"]
    )
    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "automatic-orchestration"
    ) as tui:
        tui.boot()
        _review(tui, session, "claude", first)
        _compact(tui, session, "claude")
        second = ReviewTask(
            session,
            "followup",
            ["skills_download.py", "codex_config.py", "smart_routing/session_env.py"],
        )
        _review(tui, session, "claude", second)
        tui.exit_normally()


@pytest.mark.codex
@pytest.mark.managed_fixture
def test_orchestrator_codex_delegates_and_recovers_after_compaction(
    live_session, workspace, tmp_path
):
    """Scenario: opt into orchestration and ask Codex to review three source modules,
    without requesting subagents; compact natively, then review three different modules.

    Expected: the prompt and compaction hooks deliver the complete workflow; both reviews
    have a completed native child and a root report containing values read from all files.
    Parent history copied into a child is excluded from child-completion evidence.
    """
    session = live_session
    session.env.update(
        ENABLE_SMART_ROUTING_V2="1",
        ENABLE_SMART_ROUTING_SUBAGENT_ONLY="1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR="1",
        TMPDIR=str(tmp_path),
    )
    use_managed_config_fixture(session, "codex_smart_routing")
    session.run(
        "configure", "--workspace", workspace, "--skip-upgrade", "--disable-databricks-ai-tools"
    )
    first = ReviewTask(
        session, "initial", ["config_io.py", "gateway_proxy.py", "managed_config.py"]
    )
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "automatic-orchestration"
    ) as tui:
        tui.boot()
        _review(tui, session, "codex", first)
        _compact(tui, session, "codex")
        second = ReviewTask(
            session,
            "followup",
            ["skills_download.py", "codex_config.py", "smart_routing/session_env.py"],
        )
        _review(tui, session, "codex", second)
        tui.exit_normally()
