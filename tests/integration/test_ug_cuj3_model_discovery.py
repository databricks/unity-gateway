"""CUJ3: complete managed, schema-scoped model discovery journeys."""

import json
import os
import tomllib

import pytest
from utils.cuj3 import (
    CLAUDE_DECOY,
    CLAUDE_DEFAULT,
    CLAUDE_MODELS,
    CODEX_DECOY,
    CODEX_DEFAULT,
    CODEX_MODELS,
    GEMINI_MODEL,
    MODEL_HEADER,
    MODEL_SCHEMA,
    MODEL_SERVICES,
    OTHER_MODEL_SCHEMA,
    assert_native_model_identity,
    assert_persisted_config,
    assert_picker_inventory,
    claude_discovery_model_id,
    claude_model_service_id,
    codex_model_in_picker,
    fetch_claude_parent_catalog,
    fetch_codex_parent_catalog,
    fetch_model_service_inventory,
)
from utils.evidence import FileTask
from utils.model_discovery import claude_model_in_picker
from utils.provider_catalog import parse_codex_provider_catalog
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.managed, pytest.mark.cuj3, pytest.mark.workspace_isolated]


@pytest.mark.claude
@pytest.mark.tui
def test_case_03_managed_schema_pointers_claude(live_session, workspace):
    """Scenario: configure Claude from the published CUJ3 model schema, then use every model.

    Expected: public configure persists the exact managed schema/defaults. An independent
    UC GETs prove all five in-scope services, including Gemini, and both decoys exist.
    An independent compatible inventory proves scoped models and the accessible decoy;
    generated settings, native gateway cache, and exact numbered picker inventory match the scoped
    inventory. Bare ug and explicit ug claude each complete a separate real Claude/Sonnet
    TUI file task with native exact default identity and normal exit, without model overrides;
    ug claude -p completes a Sonnet print task; every extra model completes a task with native identity. In-scope
    Gemini and both out-of-scope decoys are excluded. These intended compatibility assertions
    fail if the live backend differs; they are not a claim of live verification.
    """
    session = live_session
    configured = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    assert_persisted_config(session, workspace)

    bearer = os.environ["DATABRICKS_BEARER"]
    inventory = fetch_model_service_inventory(workspace, bearer)
    session.record("cuj3-model-service-sources.json", inventory)
    assert set(inventory) == MODEL_SERVICES | {CLAUDE_DECOY, CODEX_DECOY}, inventory
    parent_catalog = fetch_claude_parent_catalog(workspace, bearer, MODEL_SCHEMA)
    decoy_catalog = fetch_claude_parent_catalog(workspace, bearer, OTHER_MODEL_SCHEMA)
    session.record(
        "cuj3-parent-inventory.json",
        {
            "scoped": parent_catalog.model_ids,
            "decoy": decoy_catalog.model_ids,
            "display_names": parent_catalog.display_names,
            "pages": parent_catalog.payloads,
            "decoy_pages": decoy_catalog.payloads,
        },
    )
    service_ids = [claude_model_service_id(model) for model in parent_catalog.model_ids]
    assert len(service_ids) == len(set(service_ids)), parent_catalog
    assert set(service_ids) == CLAUDE_MODELS, parent_catalog
    assert set(parent_catalog.model_ids) == {
        claude_discovery_model_id(model) for model in CLAUDE_MODELS
    }, parent_catalog
    decoy_ids = [claude_model_service_id(model) for model in decoy_catalog.model_ids]
    assert len(decoy_ids) == len(set(decoy_ids)), decoy_catalog
    assert set(decoy_ids) == {CLAUDE_DECOY}, decoy_catalog
    assert set(decoy_catalog.model_ids) == {claude_discovery_model_id(CLAUDE_DECOY)}, decoy_catalog

    default_task = FileTask(session)
    with AgentTerminal(
        session, "claude", [str(session.binary)], "cuj3-bare-ug-claude-default-and-picker"
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
            .count(f"{MODEL_HEADER}: {MODEL_SCHEMA}")
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
        session.record("cuj3-claude-settings.json", settings)
        picker_screen = tui.open_model_picker(
            model_visible=lambda screen: all(
                claude_model_in_picker(screen, model, parent_catalog.display_names[model])
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
    assert_native_model_identity(session, "claude", default_task.value, CLAUDE_DEFAULT)

    explicit_task = FileTask(session)
    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "cuj3-explicit-ug-claude-default"
    ) as tui:
        tui.boot()
        tui.submit(explicit_task.prompt)
        tui.wait_for_task(explicit_task, timeout=240)
        tui.exit_normally()
    explicit_task.assert_completed(session, "claude")
    assert_native_model_identity(session, "claude", explicit_task.value, CLAUDE_DEFAULT)

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
    assert_native_model_identity(session, "claude", print_task.value, CLAUDE_DEFAULT)

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
        assert_native_model_identity(session, "claude", task.value, model)
    session.assert_not_routed()


@pytest.mark.codex
@pytest.mark.tui
def test_case_04_managed_schema_pointers_codex(live_session, workspace):
    """Scenario: configure Codex from the published CUJ3 model schema, then use every model.

    Expected: public configure persists the exact managed schema/defaults. An independent
    UC GETs prove all five in-scope services, including Gemini, and both decoys exist.
    Independent compatible inventories, generated catalogs, real model/list protocol results,
    and the exact numbered native /model picker inventory agree. A default TUI task, a default exec task,
    and every extra-model task complete with native identity. Codex -p is a native profile
    option, not print mode: headless tasks use ug codex -- exec. In-scope Gemini and both
    out-of-scope decoys are excluded. Intended compatibility fails if the backend differs;
    these expectations are not a claim of live verification.
    """
    session = live_session
    configured = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    assert_persisted_config(session, workspace)

    bearer = os.environ["DATABRICKS_BEARER"]
    inventory = fetch_model_service_inventory(workspace, bearer)
    session.record("cuj3-model-service-sources.json", inventory)
    assert set(inventory) == MODEL_SERVICES | {CLAUDE_DECOY, CODEX_DECOY}, inventory
    parent_catalog = fetch_codex_parent_catalog(workspace, bearer, MODEL_SCHEMA)
    decoy_catalog = fetch_codex_parent_catalog(workspace, bearer, OTHER_MODEL_SCHEMA)
    session.record(
        "cuj3-parent-inventory.json",
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
    session.record("cuj3-codex-catalog.json", catalog)

    default_task = FileTask(session)
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "cuj3-codex-default"
    ) as tui:
        tui.boot()
        config = tomllib.loads((session.home / ".codex/ucode.config.toml").read_text())
        assert config.get("model") == CODEX_DEFAULT, config
        session.record("cuj3-codex-config.json", config)
        picker_screen = tui.open_codex_model_picker(
            model_visible=lambda screen: all(
                codex_model_in_picker(screen, model, display_names[model])
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
    assert_native_model_identity(session, "codex", default_task.value, CODEX_DEFAULT)

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
    assert_native_model_identity(session, "codex", exec_task.value, CODEX_DEFAULT)

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
        assert_native_model_identity(session, "codex", task.value, model)
    session.assert_not_routed()
