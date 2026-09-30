"""Integration CUJ for the hidden `--suppress-managed-config` launch flag.

`--suppress-managed-config` skips the workspace managed config for a launch, so a published admin
config can't change that launch. The admin CodingAgentConfig is injected via
UCODE_MANAGED_CONFIG_STUB (tests/AGENTS.md rule 4) so the assertions don't depend on the live
workspace publishing a config shaped like this; the real `ug claude` / `ug codex` launch path,
auth, and settings writers stay real.
"""

import json

import pytest
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
    set_managed_config_stub,
)
from utils.terminal import TerminalProcess

CLAUDE_OPUS = "system.ai.claude-opus-4-8"
# A real ca-central model absent from the live workspace's native discovery (see
# test_managed_fixture_claude_model_picker_reflects_the_config in test_ug_configure_managed_models.py):
# its presence in Claude's settings can only come from the injected managed config.
CLAUDE_OFF_MENU = "system.ai.claude-sonnet-5"
CODEX_DEFAULT = "system.ai.gpt-5-6-sol"
# Not a real model id; only the injected managed config can put it in Codex's catalog (see
# CODEX_WITHOUT_BUNDLED_METADATA in test_ug_configure_managed_models.py).
CODEX_ADMIN_ONLY = "system.ai.gpt-99"

SUPPRESSION_NOTE = "Skipping the workspace's managed config; using your own settings"


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_suppress_managed_config(live_session, workspace, tmp_path):
    """Scenario: the workspace publishes a managed Claude model list. The developer launches
    `ug claude` once normally, then again with the hidden `--suppress-managed-config` flag.

    Expected: the ordinary launch writes the admin's static model list into
    `~/.claude/ucode-settings.json`'s `availableModels`, including a model real Claude discovery
    never lists on its own. The same launch with `--suppress-managed-config` clears
    `availableModels` entirely (falling back to the developer's own native gateway discovery) even
    though the workspace still publishes the config, while the launch itself still completes with a
    clean exit (auth and startup succeed).
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE", build_claude_agent_config([CLAUDE_OPUS, CLAUDE_OFF_MENU])
    )
    set_managed_config_stub(session, tmp_path, config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    settings_path = session.home / ".claude" / "ucode-settings.json"

    applied_command = [str(session.binary), "claude", "--", "--version"]
    with TerminalProcess(
        session, "claude", applied_command, "suppress-managed-config-applied"
    ) as terminal:
        terminal.finish(timeout=240)
    applied_settings = json.loads(settings_path.read_text())
    assert applied_settings.get("availableModels") == [CLAUDE_OPUS, CLAUDE_OFF_MENU], (
        applied_settings
    )

    suppressed_command = [
        str(session.binary),
        "claude",
        "--suppress-managed-config",
        "--",
        "--version",
    ]
    with TerminalProcess(
        session, "claude", suppressed_command, "suppress-managed-config-suppressed"
    ) as terminal:
        terminal.finish(timeout=240)
    suppressed_settings = json.loads(settings_path.read_text())
    assert suppressed_settings.get("availableModels") is None, suppressed_settings


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_managed_fixture_codex_suppress_managed_config(live_session, workspace, tmp_path):
    """Scenario: the workspace publishes a managed Codex model list naming a nonexistent GPT
    model. The developer launches `ug codex` once normally, then again with the hidden
    `--suppress-managed-config` flag.

    Expected: the ordinary launch writes the admin's static catalog, including the admin-only
    model, to `~/.ucode/codex-model-catalog.json` and prints no suppression note. The same launch
    with `--suppress-managed-config` deletes that catalog file entirely, falls back to the
    developer's own real model discovery, and prints ug's suppression note, even though the
    workspace still publishes the config, while auth and the launch itself still succeed (exit 0).
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CODEX",
        build_codex_agent_config(models=[CODEX_DEFAULT, CODEX_ADMIN_ONLY]),
    )
    set_managed_config_stub(session, tmp_path, config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    catalog_path = session.home / ".ucode" / "codex-model-catalog.json"

    applied = session.run("codex", "--", "--version", timeout=240)
    assert SUPPRESSION_NOTE not in applied.stdout, applied.stdout
    applied_catalog = json.loads(catalog_path.read_text())
    applied_models = [entry.get("slug") for entry in applied_catalog.get("models", [])]
    assert CODEX_ADMIN_ONLY in applied_models, applied_catalog

    suppressed = session.run("codex", "--suppress-managed-config", "--", "--version", timeout=240)
    assert SUPPRESSION_NOTE in suppressed.stdout, suppressed.stdout
    assert not catalog_path.exists(), "expected the managed catalog to be removed when suppressed"
