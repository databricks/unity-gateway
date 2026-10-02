"""Managed catalog discovery, default launches, and explicit model selection."""

import json
import os

import pytest
from catalog_discovery_expectations import (
    CLAUDE_DECOY,
    CLAUDE_DEFAULT,
    CLAUDE_MODELS,
    CODEX_DECOY,
    CODEX_DEFAULT,
    CODEX_MODELS,
    GEMINI_MODEL,
    MODEL_SCHEMA,
    OTHER_MODEL_SCHEMA,
)
from utils.agents import claude, codex
from utils.evidence import FileTask, assert_completed_task_model
from utils.model_discovery import assert_picker_inventory
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.managed, pytest.mark.catalog_discovery, pytest.mark.workspace_isolated]


def _claude_file_task(session):
    task = FileTask(session)
    task.prompt = (
        f"Use the Read tool to read {session.cwd / task.filename}. Reply with only its contents."
    )
    return task


def _assert_claude_headless_model(result, expected):
    final = None
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "result":
            final = payload
    assert final is not None and not final.get("is_error"), result.stdout
    usage = final["modelUsage"]
    assert set(usage) == {expected}, {"expected": expected, "observed": sorted(usage)}
    assert usage[expected]["outputTokens"] > 0, usage


@pytest.mark.claude
@pytest.mark.tui
def test_catalog_discovery_claude_picker_preserves_default(live_session, workspace):
    """Scenario: configure, launch ug claude, inspect its picker, then submit a task.

    Expected: only Sonnet/Haiku/Kimi appear; Gemini and decoys are absent.
    Dismissing the picker without a selection preserves Sonnet for the completed task.
    """
    session = live_session
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)

    bearer = os.environ["DATABRICKS_BEARER"]
    parent_catalog = claude.fetch_parent_catalog(workspace, bearer, MODEL_SCHEMA)
    decoy_catalog = claude.fetch_parent_catalog(workspace, bearer, OTHER_MODEL_SCHEMA)
    assert set(parent_catalog.model_ids) == {
        claude.discovery_model_id(model) for model in CLAUDE_MODELS
    }, parent_catalog
    assert set(decoy_catalog.model_ids) == {claude.discovery_model_id(CLAUDE_DECOY)}, decoy_catalog

    task = _claude_file_task(session)
    with AgentTerminal(
        session,
        "claude",
        [str(session.binary), "claude"],
        "catalog-discovery-claude-picker",
    ) as tui:
        tui.boot()
        picker_screen = tui.open_model_picker(
            model_visible=lambda screen: all(
                claude.model_in_picker(screen, model, parent_catalog.display_names[model])
                for model in parent_catalog.model_ids
            )
        )
        assert_picker_inventory(picker_screen, "claude", parent_catalog.display_names)
        for excluded in (GEMINI_MODEL, CLAUDE_DECOY, CODEX_DECOY):
            assert excluded not in picker_screen, picker_screen
            assert excluded.rsplit(".", 1)[-1] not in picker_screen, picker_screen
        assert f"{CLAUDE_DEFAULT} · api usage billing" in tui.visible.casefold(), tui.visible
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=240)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    session.assert_not_routed()


@pytest.mark.codex
@pytest.mark.tui
def test_catalog_discovery_codex_picker_preserves_default(live_session, workspace):
    """Scenario: configure, query Codex model/list, inspect its TUI picker, then submit a task.

    Expected: model/list and the picker expose only GPT Luna/Kimi, not Gemini or decoys.
    Dismissing the picker preserves GPT Luna for the completed task's selected model.
    """
    session = live_session
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)

    bearer = os.environ["DATABRICKS_BEARER"]
    parent_catalog = codex.fetch_parent_catalog(workspace, bearer, MODEL_SCHEMA)
    decoy_catalog = codex.fetch_parent_catalog(workspace, bearer, OTHER_MODEL_SCHEMA)
    assert set(parent_catalog.model_ids) == CODEX_MODELS, parent_catalog
    assert set(decoy_catalog.model_ids) == {CODEX_DECOY}, decoy_catalog

    models = session.codex_model_ids(["app-server", "--listen", "stdio://"])
    assert len(models) == len(set(models)), models
    assert set(models) == CODEX_MODELS, models
    display_names = {
        entry["slug"]: entry.get("display_name") or entry["slug"]
        for page in parent_catalog.payloads
        for entry in page["models"]
        if entry["slug"] in CODEX_MODELS
    }

    task = FileTask(session)
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "catalog-discovery-codex-picker"
    ) as tui:
        tui.boot()
        picker_screen = tui.open_codex_model_picker(
            model_visible=lambda screen: all(
                codex.model_in_picker(screen, model, display_names[model])
                for model in parent_catalog.model_ids
            )
        )
        assert_picker_inventory(picker_screen, "codex", display_names)
        for excluded in (GEMINI_MODEL, CLAUDE_DECOY, CODEX_DECOY):
            assert excluded not in picker_screen, picker_screen
            assert excluded.rsplit(".", 1)[-1] not in picker_screen, picker_screen
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=240)
        tui.exit_normally()
    assert_completed_task_model(session, "codex", task.value, CODEX_DEFAULT)
    session.assert_not_routed()


@pytest.mark.claude
@pytest.mark.tui
def test_catalog_discovery_bare_ug_uses_claude_default(live_session, workspace):
    """Scenario: configure the managed workspace, launch bare ug, and submit its first task.

    Expected: Claude completes the task on Sonnet without an agent or model override.
    """
    session = live_session
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    task = _claude_file_task(session)
    with AgentTerminal(
        session, "claude", [str(session.binary)], "catalog-discovery-bare-ug-default"
    ) as tui:
        tui.boot()
        assert f"{CLAUDE_DEFAULT} · api usage billing" in tui.visible.casefold(), tui.visible
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=240)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    session.assert_not_routed()


@pytest.mark.claude
@pytest.mark.tui
def test_catalog_discovery_claude_tui_uses_default(live_session, workspace):
    """Scenario: configure, launch ug claude, and submit its first task without opening a picker.

    Expected: Claude completes the task on the configured Sonnet default without overrides.
    """
    session = live_session
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    task = _claude_file_task(session)
    with AgentTerminal(
        session,
        "claude",
        [str(session.binary), "claude"],
        "catalog-discovery-claude-tui-default",
    ) as tui:
        tui.boot()
        assert f"{CLAUDE_DEFAULT} · api usage billing" in tui.visible.casefold(), tui.visible
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=240)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    session.assert_not_routed()


@pytest.mark.codex
@pytest.mark.tui
def test_catalog_discovery_codex_tui_uses_default(live_session, workspace):
    """Scenario: configure, launch ug codex, and submit its first task without opening a picker.

    Expected: Codex completes the task with GPT Luna selected, without a model override.
    """
    session = live_session
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    task = FileTask(session)
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "catalog-discovery-codex-tui-default"
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=240)
        tui.exit_normally()
    assert_completed_task_model(session, "codex", task.value, CODEX_DEFAULT)
    session.assert_not_routed()


@pytest.mark.claude
def test_catalog_discovery_claude_headless_uses_default(live_session, workspace):
    """Scenario: configure, then run ug claude -p with a file task and no model override.

    Expected: the print task completes with a Sonnet response.
    """
    session = live_session
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    task = _claude_file_task(session)
    result = session.run(
        "claude",
        "-p",
        task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        timeout=240,
    )
    task.assert_headless_answer("claude", result)
    _assert_claude_headless_model(result, CLAUDE_DEFAULT)
    session.assert_not_routed()


@pytest.mark.codex
def test_catalog_discovery_codex_headless_uses_default(live_session, workspace):
    """Scenario: configure, then run ug codex -- exec with a file task and no model override.

    Expected: the exec task completes with GPT Luna selected.
    """
    session = live_session
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    task = FileTask(session)
    result = session.run(
        "codex", "--", "exec", "--skip-git-repo-check", "--json", task.prompt, timeout=240
    )
    task.assert_headless_answer("codex", result)
    assert_completed_task_model(session, "codex", task.value, CODEX_DEFAULT)
    session.assert_not_routed()


@pytest.mark.claude
@pytest.mark.parametrize("model", sorted(CLAUDE_MODELS - {CLAUDE_DEFAULT}))
def test_catalog_discovery_claude_explicit_model_completes_task(live_session, workspace, model):
    """Scenario: configure, then run a Claude print task with another compatible scoped model.

    Expected: each Haiku/Kimi task completes with the explicitly requested model.
    """
    session = live_session
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    task = _claude_file_task(session)
    result = session.run(
        "claude",
        "--",
        "-p",
        task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        "--model",
        claude.discovery_model_id(model),
        timeout=240,
    )
    task.assert_headless_answer("claude", result)
    _assert_claude_headless_model(result, claude.discovery_model_id(model))
    session.assert_not_routed()


@pytest.mark.codex
@pytest.mark.parametrize("model", sorted(CODEX_MODELS - {CODEX_DEFAULT}))
def test_catalog_discovery_codex_explicit_model_completes_task(live_session, workspace, model):
    """Scenario: configure, then run a Codex exec task with another compatible scoped model.

    Expected: the Kimi task completes with the explicitly requested model selected.
    """
    session = live_session
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    task = FileTask(session)
    result = session.run(
        "codex",
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        task.prompt,
        "--model",
        model,
        timeout=240,
    )
    task.assert_headless_answer("codex", result)
    assert_completed_task_model(session, "codex", task.value, model)
    session.assert_not_routed()
