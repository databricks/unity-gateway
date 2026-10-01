"""CUJ3 journeys for managed Unity Catalog model, MCP, and skill locations."""

from __future__ import annotations

import json
import os
import re

import pytest
from utils.cuj3 import (
    CLAUDE_DECOY,
    CLAUDE_MODELS,
    CODEX_DECOY,
    CODEX_MODELS,
    MCP_DECOY,
    MCP_SCHEMA,
    MCP_SERVERS,
    MODEL_SCHEMA,
    OTHER_MCP_SCHEMA,
    OTHER_MODEL_SCHEMA,
    OTHER_SKILL_SCHEMA,
    SKILL_DECOY,
    SKILL_SCHEMA,
    SKILLS,
    assert_cuj3_config,
    assert_native_model_identity,
    assert_persisted_config,
    assert_skill_transcript,
    fetch_claude_parent_catalog,
    fetch_codex_parent_catalog,
    fetch_mcp_names,
    fetch_skill_names,
    fixture_value,
    native_tool_evidence,
)
from utils.evidence import FileTask, agent_sessions, assert_no_terminal_api_error, assistant_answers
from utils.managed import fetch_published_managed_config
from utils.terminal import AgentTerminal

pytestmark = [
    pytest.mark.managed,
    pytest.mark.cuj3,
    pytest.mark.workspace_isolated,
]
MCP_PERMISSION_TARGETS = (
    (MCP_SERVERS["fixture_reader"], "read_fixture"),
    (MCP_SERVERS["fixture_metadata"], "describe_fixture"),
)


def _assistant_answers(session, agent: str) -> list[str]:
    return [
        answer
        for records in agent_sessions(session, agent).values()
        for answer in assistant_answers(agent, records)
    ]


def _wait_for_native_answer(
    tui: AgentTerminal,
    session,
    agent: str,
    expected_parts: tuple[str, ...],
    permission_targets: tuple[tuple[str, str], ...],
) -> None:
    """Wait for an assistant result, approving only the fixture MCP calls if asked."""
    permission_in_progress = False

    def completed(screen: str) -> bool:
        nonlocal permission_in_progress
        assert_no_terminal_api_error(screen)
        lowered = screen.casefold()
        if "Do you want to proceed?" in screen:
            matching = [
                tool
                for server, tool in permission_targets
                if server.casefold() in lowered and tool.casefold() in lowered
            ]
            selected_yes = re.search(r"(?m)^\s*[›❯>]\s*1[.)]\s*(?:Yes|Allow)\b", screen)
            assert matching and selected_yes, "Unrecognized fixture tool permission:\n" + screen
            if not permission_in_progress:
                tui.send("\r", f"allow expected fixture MCP tool {matching[0]}")
                permission_in_progress = True
            return False
        permission_in_progress = False
        answers = _assistant_answers(session, agent)
        return any(all(part in answer for part in expected_parts) for answer in answers)

    tui.wait_for(completed, "the native assistant result", timeout=240)


def _assert_downloaded_skills(session) -> None:
    for root_name in (".claude/skills", ".agents/skills"):
        root = session.home / root_name
        assert root.is_dir(), root
        bundles = {path.name for path in root.iterdir() if path.is_dir()}
        assert bundles == SKILLS, (root, bundles)
        for bundle in SKILLS:
            assert (root / bundle / "SKILL.md").is_file(), root / bundle / "SKILL.md"
        assert SKILL_DECOY not in bundles


def _assert_mcp_list(output: str) -> None:
    for name in MCP_SERVERS.values():
        row = next((line for line in output.splitlines() if name in line), "")
        assert row and "managed" in row, output
        assert "connected" in row or "enabled" in row, row
    assert MCP_DECOY not in output, output


@pytest.mark.claude
def test_case_03_managed_schema_pointers_claude(live_session, workspace):
    """Scenario: configure Claude from CUJ3's published Unity Catalog locations.

    Expected: Claude sees exactly the scoped model, MCP, and skill inventory, completes tasks on
    both managed models with native model identity, invokes both fixture tools, and executes both
    downloaded skills while accessible decoys remain absent.
    """
    session = live_session
    bearer = os.environ["DATABRICKS_BEARER"]
    published = fetch_published_managed_config(workspace, bearer)
    assert_cuj3_config(published)

    parent_catalog = fetch_claude_parent_catalog(workspace, bearer, MODEL_SCHEMA)
    decoy_catalog = fetch_claude_parent_catalog(workspace, bearer, OTHER_MODEL_SCHEMA)
    assert set(parent_catalog.model_ids) == CLAUDE_MODELS, parent_catalog
    assert set(decoy_catalog.model_ids) == {CLAUDE_DECOY}, decoy_catalog

    skills = fetch_skill_names(workspace, bearer, SKILL_SCHEMA)
    decoy_skills = fetch_skill_names(workspace, bearer, OTHER_SKILL_SCHEMA)
    assert skills == SKILLS, skills
    assert decoy_skills == {SKILL_DECOY}, decoy_skills
    scoped_mcps = fetch_mcp_names(workspace, bearer, MCP_SCHEMA)
    decoy_mcps = fetch_mcp_names(workspace, bearer, OTHER_MCP_SCHEMA)
    assert scoped_mcps == {f"{MCP_SCHEMA}.{leaf}" for leaf in MCP_SERVERS}, scoped_mcps
    assert decoy_mcps == {f"{OTHER_MCP_SCHEMA}.fixture_decoy"}, decoy_mcps
    session.record(
        "cuj3-parent-inventory.json",
        {
            "claude": {
                "scoped": parent_catalog.model_ids,
                "decoy": decoy_catalog.model_ids,
                "pages": parent_catalog.payloads,
            },
            "skills": {"scoped": sorted(skills), "decoy": sorted(decoy_skills)},
            "mcps": {"scoped": sorted(scoped_mcps), "decoy": sorted(decoy_mcps)},
        },
    )

    configured = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        timeout=300,
    )
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    assert_persisted_config(session, workspace, published)
    _assert_downloaded_skills(session)

    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "cuj3-claude-model-picker"
    ) as tui:
        tui.boot()
        picker_screen = tui.open_model_picker(
            model_visible=lambda text: any(model in text for model in CLAUDE_MODELS)
        )
        tui.exit_normally()
    assert all(model in picker_screen for model in CLAUDE_MODELS), picker_screen
    assert CLAUDE_DECOY not in picker_screen, picker_screen

    gateway_ids = set(session.claude_gateway_model_ids())
    assert gateway_ids == CLAUDE_MODELS, gateway_ids
    assert CLAUDE_DECOY not in gateway_ids
    settings_path = session.home / ".claude" / "ucode-settings.json"
    settings = json.loads(settings_path.read_text())
    options = (settings.get("modelPicker") or {}).get("options")
    assert isinstance(options, list), settings
    picker_ids = {option.get("model") for option in options if isinstance(option, dict)}
    assert picker_ids == CLAUDE_MODELS, settings
    assert CLAUDE_DECOY not in picker_ids

    default_task = FileTask(session)
    default_result = session.run(
        "claude",
        "--",
        "-p",
        default_task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        timeout=240,
    )
    default_task.assert_headless_answer("claude", default_result)
    assert_native_model_identity(
        session,
        "claude",
        default_task.value,
        f"{MODEL_SCHEMA}.claude_sonnet",
    )

    extra_task = FileTask(session)
    extra_result = session.run(
        "claude",
        "--",
        "-p",
        extra_task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        "--model",
        f"{MODEL_SCHEMA}.claude_extra",
        timeout=240,
    )
    extra_task.assert_headless_answer("claude", extra_result)
    assert_native_model_identity(
        session,
        "claude",
        extra_task.value,
        f"{MODEL_SCHEMA}.claude_extra",
    )

    run_id = "cuj3-claude-" + os.urandom(8).hex()
    reader_value = fixture_value("read_fixture", run_id)
    metadata_value = fixture_value("describe_fixture", run_id)
    summary_id = "cuj3-summary-" + os.urandom(8).hex()
    summary_value = fixture_value("read_fixture", summary_id)
    audit_id = "cuj3-audit-" + os.urandom(8).hex()
    audit_reader = fixture_value("read_fixture", audit_id)
    audit_metadata = fixture_value("describe_fixture", audit_id)
    mcp_prompt = (
        f"On server {MCP_SERVERS['fixture_reader']}, call read_fixture with run_id {run_id}. "
        f"Then on server {MCP_SERVERS['fixture_metadata']}, call describe_fixture with the same "
        "run_id. Return the two opaque tool results with labels reader= and metadata=. Do not "
        "calculate or infer either value."
    )
    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "cuj3-claude-tools-skills"
    ) as tui:
        tui.boot()
        tui.submit(mcp_prompt)
        _wait_for_native_answer(
            tui,
            session,
            "claude",
            (f"reader={reader_value}", f"metadata={metadata_value}"),
            MCP_PERMISSION_TARGETS,
        )
        tui.submit(f"/fixture-summary {summary_id}")
        _wait_for_native_answer(
            tui,
            session,
            "claude",
            (f"fixture-summary: {summary_value}",),
            MCP_PERMISSION_TARGETS,
        )
        tui.submit(f"/fixture-audit {audit_id}")
        _wait_for_native_answer(
            tui,
            session,
            "claude",
            (f"fixture-audit: {audit_reader} {audit_metadata}",),
            MCP_PERMISSION_TARGETS,
        )
        tui.exit_normally()

    native_tool_evidence(
        session,
        "claude",
        run_id,
        MCP_SERVERS["fixture_reader"],
        "read_fixture",
        reader_value,
    )
    native_tool_evidence(
        session,
        "claude",
        run_id,
        MCP_SERVERS["fixture_metadata"],
        "describe_fixture",
        metadata_value,
    )
    native_tool_evidence(
        session, "claude", summary_id, MCP_SERVERS["fixture_reader"], "read_fixture", summary_value
    )
    native_tool_evidence(
        session, "claude", audit_id, MCP_SERVERS["fixture_reader"], "read_fixture", audit_reader
    )
    native_tool_evidence(
        session,
        "claude",
        audit_id,
        MCP_SERVERS["fixture_metadata"],
        "describe_fixture",
        audit_metadata,
    )
    assert_skill_transcript(session, "claude", "fixture-summary", "read_fixture", summary_id)
    assert_skill_transcript(session, "claude", "fixture-audit", "read_fixture", audit_id)
    assert any(
        "fixture-audit:" in answer and audit_metadata in answer
        for answer in _assistant_answers(session, "claude")
    )

    mcp_list = session.run("mcp", "list", timeout=180)
    _assert_mcp_list(mcp_list.stdout)
    status = session.run("status", timeout=180)
    assert len(re.findall(r"MCP servers:\s+2\b", status.stdout)) >= 2, status.stdout
    assert len(re.findall(r"Skills:\s+2\b", status.stdout)) >= 2, status.stdout
    skills_list = session.run("skills", "list", timeout=180)
    assert all(skill in skills_list.stdout for skill in SKILLS), skills_list.stdout
    assert SKILL_DECOY not in skills_list.stdout, skills_list.stdout
    session.assert_not_routed()


@pytest.mark.codex
def test_case_04_managed_schema_pointers_codex(live_session, workspace):
    """Scenario: configure Codex from CUJ3's published Unity Catalog locations.

    Expected: Codex sees exactly the scoped model, MCP, and skill inventory, completes tasks on
    both managed models with native model identity, invokes both fixture tools, and executes both
    downloaded skills while accessible decoys remain absent.
    """
    session = live_session
    bearer = os.environ["DATABRICKS_BEARER"]
    published = fetch_published_managed_config(workspace, bearer)
    assert_cuj3_config(published)

    parent_catalog = fetch_codex_parent_catalog(workspace, bearer, MODEL_SCHEMA)
    decoy_catalog = fetch_codex_parent_catalog(workspace, bearer, OTHER_MODEL_SCHEMA)
    assert set(parent_catalog.model_ids) == CODEX_MODELS, parent_catalog
    assert set(decoy_catalog.model_ids) == {CODEX_DECOY}, decoy_catalog

    skills = fetch_skill_names(workspace, bearer, SKILL_SCHEMA)
    decoy_skills = fetch_skill_names(workspace, bearer, OTHER_SKILL_SCHEMA)
    assert skills == SKILLS, skills
    assert decoy_skills == {SKILL_DECOY}, decoy_skills
    scoped_mcps = fetch_mcp_names(workspace, bearer, MCP_SCHEMA)
    decoy_mcps = fetch_mcp_names(workspace, bearer, OTHER_MCP_SCHEMA)
    assert scoped_mcps == {f"{MCP_SCHEMA}.{leaf}" for leaf in MCP_SERVERS}, scoped_mcps
    assert decoy_mcps == {f"{OTHER_MCP_SCHEMA}.fixture_decoy"}, decoy_mcps
    session.record(
        "cuj3-parent-inventory.json",
        {
            "codex": {
                "scoped": parent_catalog.model_ids,
                "decoy": decoy_catalog.model_ids,
                "payloads": parent_catalog.payloads,
            },
            "skills": {"scoped": sorted(skills), "decoy": sorted(decoy_skills)},
            "mcps": {"scoped": sorted(scoped_mcps), "decoy": sorted(decoy_mcps)},
        },
    )

    configured = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        timeout=300,
    )
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    assert_persisted_config(session, workspace, published)
    _assert_downloaded_skills(session)

    codex_ids = set(session.codex_model_ids(["app-server", "--listen", "stdio://"]))
    assert codex_ids == CODEX_MODELS, codex_ids
    assert CODEX_DECOY not in codex_ids

    default_task = FileTask(session)
    default_result = session.run(
        "codex",
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        default_task.prompt,
        timeout=240,
    )
    default_task.assert_headless_answer("codex", default_result)
    assert_native_model_identity(
        session,
        "codex",
        default_task.value,
        f"{MODEL_SCHEMA}.codex_primary",
    )

    extra_task = FileTask(session)
    extra_result = session.run(
        "codex",
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        "--model",
        f"{MODEL_SCHEMA}.codex_extra",
        extra_task.prompt,
        timeout=240,
    )
    extra_task.assert_headless_answer("codex", extra_result)
    assert_native_model_identity(
        session,
        "codex",
        extra_task.value,
        f"{MODEL_SCHEMA}.codex_extra",
    )

    run_id = "cuj3-codex-" + os.urandom(8).hex()
    reader_value = fixture_value("read_fixture", run_id)
    metadata_value = fixture_value("describe_fixture", run_id)
    summary_id = "cuj3-summary-" + os.urandom(8).hex()
    summary_value = fixture_value("read_fixture", summary_id)
    audit_id = "cuj3-audit-" + os.urandom(8).hex()
    audit_reader = fixture_value("read_fixture", audit_id)
    audit_metadata = fixture_value("describe_fixture", audit_id)
    mcp_prompt = (
        f"On server {MCP_SERVERS['fixture_reader']}, call read_fixture with run_id {run_id}. "
        f"Then on server {MCP_SERVERS['fixture_metadata']}, call describe_fixture with the same "
        "run_id. Return the two opaque tool results with labels reader= and metadata=. Do not "
        "calculate or infer either value."
    )
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "cuj3-codex-tools-skills"
    ) as tui:
        tui.boot()
        tui.submit(mcp_prompt)
        _wait_for_native_answer(
            tui,
            session,
            "codex",
            (f"reader={reader_value}", f"metadata={metadata_value}"),
            MCP_PERMISSION_TARGETS,
        )
        tui.submit(f"$fixture-summary {summary_id}")
        _wait_for_native_answer(
            tui,
            session,
            "codex",
            (f"fixture-summary: {summary_value}",),
            MCP_PERMISSION_TARGETS,
        )
        tui.submit(f"$fixture-audit {audit_id}")
        _wait_for_native_answer(
            tui,
            session,
            "codex",
            (f"fixture-audit: {audit_reader} {audit_metadata}",),
            MCP_PERMISSION_TARGETS,
        )
        tui.exit_normally()

    native_tool_evidence(
        session,
        "codex",
        run_id,
        MCP_SERVERS["fixture_reader"],
        "read_fixture",
        reader_value,
    )
    native_tool_evidence(
        session,
        "codex",
        run_id,
        MCP_SERVERS["fixture_metadata"],
        "describe_fixture",
        metadata_value,
    )
    native_tool_evidence(
        session, "codex", summary_id, MCP_SERVERS["fixture_reader"], "read_fixture", summary_value
    )
    native_tool_evidence(
        session, "codex", audit_id, MCP_SERVERS["fixture_reader"], "read_fixture", audit_reader
    )
    native_tool_evidence(
        session,
        "codex",
        audit_id,
        MCP_SERVERS["fixture_metadata"],
        "describe_fixture",
        audit_metadata,
    )
    assert_skill_transcript(session, "codex", "fixture-summary", "read_fixture", summary_id)
    assert_skill_transcript(session, "codex", "fixture-audit", "read_fixture", audit_id)
    assert any(
        "fixture-audit:" in answer and audit_metadata in answer
        for answer in _assistant_answers(session, "codex")
    )

    mcp_list = session.run("mcp", "list", timeout=180)
    _assert_mcp_list(mcp_list.stdout)
    status = session.run("status", timeout=180)
    assert len(re.findall(r"MCP servers:\s+2\b", status.stdout)) >= 2, status.stdout
    assert len(re.findall(r"Skills:\s+2\b", status.stdout)) >= 2, status.stdout
    skills_list = session.run("skills", "list", timeout=180)
    assert all(skill in skills_list.stdout for skill in SKILLS), skills_list.stdout
    assert SKILL_DECOY not in skills_list.stdout, skills_list.stdout
    session.assert_not_routed()
