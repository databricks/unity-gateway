"""Catalog discovery: complete managed, schema-scoped model discovery journeys."""

import json
import os
import tomllib

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
    assert_model_policy,
)
from utils.agents import claude, codex
from utils.evidence import FileTask, assert_completed_task_model
from utils.managed import read_persisted_managed_config
from utils.model_discovery import (
    assert_picker_inventory,
)
from utils.provider_catalog import (
    MODEL_SERVICE_PARENT_SCHEMA_HEADER,
    parse_codex_provider_catalog,
)
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.managed, pytest.mark.catalog_discovery, pytest.mark.workspace_isolated]


@pytest.mark.claude
@pytest.mark.tui
def test_catalog_discovery_claude(live_session, workspace):
    """Scenario: configure published Catalog discovery policy; launch bare ug, ug claude, and print tasks.

    Expected: persisted schema/defaults match policy; independent
    catalogs, settings, cache, and exact numbered picker agree on Sonnet/Haiku/Kimi,
    excluding Gemini and both accessible out-of-scope decoys. Both TUI launches exit
    normally after separate Sonnet file tasks; print mode uses Sonnet without an override,
    and every extra model completes a headless task, with routing off throughout.
    Claude's response-reported model must match; it does not prove the executed gateway
    backing destination. These assertions require a live pass, not collection alone.
    """
    session = live_session
    configured = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    persisted = read_persisted_managed_config(session, workspace)
    assert_model_policy(persisted["config"])
    session.record("catalog-discovery-managed-config.json", persisted)

    bearer = os.environ["DATABRICKS_BEARER"]
    parent_catalog = claude.fetch_parent_catalog(workspace, bearer, MODEL_SCHEMA)
    decoy_catalog = claude.fetch_parent_catalog(workspace, bearer, OTHER_MODEL_SCHEMA)
    session.record(
        "catalog-discovery-parent-inventory.json",
        {
            "scoped": parent_catalog.model_ids,
            "decoy": decoy_catalog.model_ids,
            "display_names": parent_catalog.display_names,
            "pages": parent_catalog.payloads,
            "decoy_pages": decoy_catalog.payloads,
        },
    )
    service_ids = [claude.model_service_id(model) for model in parent_catalog.model_ids]
    assert len(service_ids) == len(set(service_ids)), parent_catalog
    assert set(service_ids) == CLAUDE_MODELS, parent_catalog
    assert set(parent_catalog.model_ids) == {
        claude.discovery_model_id(model) for model in CLAUDE_MODELS
    }, parent_catalog
    decoy_ids = [claude.model_service_id(model) for model in decoy_catalog.model_ids]
    assert len(decoy_ids) == len(set(decoy_ids)), decoy_catalog
    assert set(decoy_ids) == {CLAUDE_DECOY}, decoy_catalog
    assert set(decoy_catalog.model_ids) == {claude.discovery_model_id(CLAUDE_DECOY)}, decoy_catalog

    default_task = FileTask(session)
    with AgentTerminal(
        session,
        "claude",
        [str(session.binary)],
        "catalog-discovery-bare-ug-claude-default-and-picker",
    ) as tui:
        tui.boot()
        settings = json.loads((session.home / ".claude/ucode-settings.json").read_text())
        env = settings["env"]
        assert env.get("ANTHROPIC_MODEL") == CLAUDE_DEFAULT, settings
        assert env.get("ANTHROPIC_DEFAULT_SONNET_MODEL") == CLAUDE_DEFAULT, settings
        assert (
            "x-databricks-use-coding-agent-mode: true"
            in env.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
        ), settings
        assert (
            env.get("ANTHROPIC_CUSTOM_HEADERS", "")
            .splitlines()
            .count(f"{MODEL_SERVICE_PARENT_SCHEMA_HEADER}: {MODEL_SCHEMA}")
            == 1
        ), settings
        assert not {"availableModels", "enforceAvailableModels"} & settings.keys(), settings
        picker = settings["modelPicker"]
        assert picker.get("replaceBuiltInOptions") is True, picker
        options = picker["options"]
        assert isinstance(options, list) and len(options) == len(parent_catalog.model_ids), picker
        picker_ids = [option["model"] for option in options]
        assert len(picker_ids) == len(set(picker_ids)), picker
        assert set(picker_ids) == set(parent_catalog.model_ids), picker
        for option in options:
            if display_name := parent_catalog.display_names[option["model"]]:
                assert option["label"] == display_name, option
        session.record("catalog-discovery-claude-settings.json", settings)
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
        tui.submit(default_task.prompt)
        tui.wait_for_task(default_task, timeout=240)
        tui.exit_normally()
    default_task.assert_completed(session, "claude")
    assert_completed_task_model(session, "claude", default_task.value, CLAUDE_DEFAULT)

    explicit_task = FileTask(session)
    with AgentTerminal(
        session,
        "claude",
        [str(session.binary), "claude"],
        "catalog-discovery-explicit-ug-claude-default",
    ) as tui:
        tui.boot()
        tui.submit(explicit_task.prompt)
        tui.wait_for_task(explicit_task, timeout=240)
        tui.exit_normally()
    explicit_task.assert_completed(session, "claude")
    assert_completed_task_model(session, "claude", explicit_task.value, CLAUDE_DEFAULT)

    gateway_ids = session.claude_gateway_model_ids()
    assert len(gateway_ids) == len(set(gateway_ids)), gateway_ids
    assert set(gateway_ids) == set(parent_catalog.model_ids), gateway_ids
    assert not {GEMINI_MODEL, CLAUDE_DECOY, CODEX_DECOY} & set(gateway_ids), gateway_ids

    print_task = FileTask(session)
    result = session.run(
        "claude",
        "-p",
        print_task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        timeout=240,
    )
    print_task.assert_headless_answer("claude", result)
    print_task.assert_completed(session, "claude")
    assert_completed_task_model(session, "claude", print_task.value, CLAUDE_DEFAULT)

    for model in parent_catalog.model_ids:
        if model == CLAUDE_DEFAULT:
            continue
        task = FileTask(session)
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
            model,
            timeout=240,
        )
        task.assert_headless_answer("claude", result)
        task.assert_completed(session, "claude")
        assert_completed_task_model(session, "claude", task.value, model)
    session.assert_not_routed()


@pytest.mark.codex
@pytest.mark.tui
def test_catalog_discovery_codex(live_session, workspace):
    """Scenario: configure published Catalog discovery policy; launch Codex TUI and ug codex -- exec.

    Expected: persisted schema/defaults match policy; independent
    catalogs, generated catalogs, model/list, and exact numbered picker agree on GPT Luna/Kimi,
    excluding Gemini and both accessible out-of-scope decoys. TUI exits normally after a
    GPT Luna file task; exec uses that default without an override, and every extra model
    completes a headless task, with routing off throughout.
    Codex's client-selected model must match; it does not prove the executed gateway
    backing destination. These assertions require a live pass, not collection alone.
    """
    session = live_session
    configured = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    persisted = read_persisted_managed_config(session, workspace)
    assert_model_policy(persisted["config"])
    session.record("catalog-discovery-managed-config.json", persisted)

    bearer = os.environ["DATABRICKS_BEARER"]
    parent_catalog = codex.fetch_parent_catalog(workspace, bearer, MODEL_SCHEMA)
    decoy_catalog = codex.fetch_parent_catalog(workspace, bearer, OTHER_MODEL_SCHEMA)
    session.record(
        "catalog-discovery-parent-inventory.json",
        {
            "scoped": parent_catalog.model_ids,
            "decoy": decoy_catalog.model_ids,
            "pages": parent_catalog.payloads,
            "decoy_pages": decoy_catalog.payloads,
        },
    )
    assert set(parent_catalog.model_ids) == CODEX_MODELS, parent_catalog
    assert set(decoy_catalog.model_ids) == {CODEX_DECOY}, decoy_catalog
    provider_ids = {
        entry.get("slug") for page in parent_catalog.payloads for entry in page["models"]
    }
    assert not {GEMINI_MODEL, CLAUDE_DECOY, CODEX_DECOY} & provider_ids, parent_catalog
    decoy_provider_ids = {
        entry.get("slug") for page in decoy_catalog.payloads for entry in page["models"]
    }
    assert not {GEMINI_MODEL, CLAUDE_DECOY} & decoy_provider_ids, decoy_catalog

    models = session.codex_model_ids(["app-server", "--listen", "stdio://"])
    assert len(models) == len(set(models)), models
    assert set(models) == set(parent_catalog.model_ids), models
    assert not {GEMINI_MODEL, CLAUDE_DECOY, CODEX_DECOY} & set(models), models
    catalog_paths = list((session.home / ".ucode").glob("codex-model-catalog-*.json"))
    assert len(catalog_paths) == 1, catalog_paths
    catalog = json.loads(catalog_paths[0].read_text())
    assert json.loads((session.home / ".ucode/codex-model-catalog.json").read_text()) == catalog
    catalog_ids = parse_codex_provider_catalog(catalog)
    assert set(catalog_ids) == set(parent_catalog.model_ids), catalog
    assert models == list(catalog_ids), (models, catalog_ids)
    all_catalog_ids = {entry["slug"] for entry in catalog["models"]}
    assert not {GEMINI_MODEL, CLAUDE_DECOY, CODEX_DECOY} & all_catalog_ids, catalog
    display_names = {}
    for entry in catalog["models"]:
        if entry["slug"] in CODEX_MODELS:
            label = entry.get("display_name")
            assert isinstance(label, str) and label.strip(), entry
            display_names[entry["slug"]] = label
    session.record("catalog-discovery-codex-catalog.json", catalog)

    default_task = FileTask(session)
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "catalog-discovery-codex-default"
    ) as tui:
        tui.boot()
        config = tomllib.loads((session.home / ".codex/ucode.config.toml").read_text())
        assert config.get("model") == CODEX_DEFAULT, config
        session.record("catalog-discovery-codex-config.json", config)
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
        tui.submit(default_task.prompt)
        tui.wait_for_task(default_task, timeout=240)
        tui.exit_normally()
    default_task.assert_completed(session, "codex")
    assert_completed_task_model(session, "codex", default_task.value, CODEX_DEFAULT)

    exec_task = FileTask(session)
    result = session.run(
        "codex",
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        exec_task.prompt,
        timeout=240,
    )
    exec_task.assert_headless_answer("codex", result)
    exec_task.assert_completed(session, "codex")
    assert_completed_task_model(session, "codex", exec_task.value, CODEX_DEFAULT)

    for model in parent_catalog.model_ids:
        if model == CODEX_DEFAULT:
            continue
        task = FileTask(session)
        result = session.run(
            "codex",
            "--",
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--model",
            model,
            task.prompt,
            timeout=240,
        )
        task.assert_headless_answer("codex", result)
        task.assert_completed(session, "codex")
        assert_completed_task_model(session, "codex", task.value, model)
    session.assert_not_routed()
