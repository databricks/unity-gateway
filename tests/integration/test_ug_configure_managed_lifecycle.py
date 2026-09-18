"""Managed-config lifecycle CUJ: an agent's model settings track the config across transitions.

A developer's workspace configuration moves through no config -> static config A -> static config B
-> model discovery via a Model Provider Service -> no config, within one home. Switching between
static configs reconciles the generated model settings to the current config (pruning models the
previous config listed); switching to an MPS clears the static list (the header routes instead); and
configuring a workspace with no managed config clears ug's managed model settings so an unmanaged
workspace never enforces a stale list.

The configs are injected via ``UCODE_MANAGED_CONFIG_STUB`` (an explicit ``null`` for the no-config
states) so the transitions run without republishing the live workspace's config; auth, the config
writers, the agent binaries, and the workspace stay real. The MPS states reference real provider
services published on the managed e2e workspace. These assert the generated files because the whole
point is file-level reconciliation across transitions, which the TUI cannot show.
"""

import json

import pytest
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
    build_mps_agent_config,
    set_managed_config_stub,
)

# Claude model ids are written to the picker verbatim, so any real system.ai ids work.
CLAUDE_A = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
CLAUDE_B = ["system.ai.claude-sonnet-4-6", "system.ai.claude-haiku-4-5"]
# Codex ids must survive the bundled-catalog build; both of these are used elsewhere in the suite.
CODEX_A = ["system.ai.gpt-5-6-sol", "system.ai.gpt-5-4-nano"]
CODEX_B = ["system.ai.gpt-5-4-nano"]
# Model Provider Services published on the managed e2e workspace for these transitions.
CLAUDE_MPS = "main.default.ci_e2e_anthropic_mps"
CODEX_MPS = "main.default.ci_e2e_openai_mps"


def _configure_managed(session, workspace):
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout


def _configure_unmanaged(session, workspace, agent):
    session.run(
        "configure", "--workspace", workspace, "--agents", agent, "--skip-upgrade", timeout=240
    )


def _claude_picker(session):
    settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    return settings.get("availableModels")


def _codex_listed(session):
    catalog_path = session.home / ".ucode" / "codex-model-catalog.json"
    if not catalog_path.exists():
        return []
    catalog = json.loads(catalog_path.read_text())
    return [m.get("slug") for m in catalog.get("models", []) if m.get("visibility") == "list"]


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_model_lifecycle(live_session, workspace, tmp_path):
    """Scenario: configure Claude across no config -> static A -> static B -> MPS -> no config,
    injecting each config (and an explicit null for the no-config states) via the stub.

    Expected: the picker's ``availableModels`` reconciles to each static config (opus, listed by A,
    is pruned when B omits it); switching to a Model Provider Service clears the picker (the header
    routes, no static list); and a workspace with no managed config also clears it, so an unmanaged
    workspace never enforces a stale list.
    """
    session = live_session

    set_managed_config_stub(session, tmp_path, None)
    _configure_unmanaged(session, workspace, "claude")
    assert _claude_picker(session) is None

    set_managed_config_stub(
        session,
        tmp_path,
        build_coding_agent_config("CODING_AGENT_CLAUDE_CODE", build_claude_agent_config(CLAUDE_A)),
    )
    _configure_managed(session, workspace)
    assert _claude_picker(session) == CLAUDE_A

    set_managed_config_stub(
        session,
        tmp_path,
        build_coding_agent_config("CODING_AGENT_CLAUDE_CODE", build_claude_agent_config(CLAUDE_B)),
    )
    _configure_managed(session, workspace)
    assert _claude_picker(session) == CLAUDE_B
    assert "system.ai.claude-opus-4-8" not in _claude_picker(session)

    # Model discovery via MPS: the header routes, so the static picker is cleared.
    set_managed_config_stub(
        session,
        tmp_path,
        build_coding_agent_config(
            "CODING_AGENT_CLAUDE_CODE",
            build_mps_agent_config("CODING_AGENT_CLAUDE_CODE", CLAUDE_MPS),
        ),
    )
    _configure_managed(session, workspace)
    assert _claude_picker(session) is None

    # Config gone: the picker stays cleared, so an unmanaged workspace enforces no stale list.
    set_managed_config_stub(session, tmp_path, None)
    _configure_unmanaged(session, workspace, "claude")
    assert _claude_picker(session) is None


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_managed_fixture_codex_model_lifecycle(live_session, workspace, tmp_path):
    """Scenario: configure Codex across no config -> static A -> static B -> MPS -> no config,
    injecting each config (and an explicit null for the no-config states) via the stub.

    Expected: the generated catalog reconciles to each static config (A's extra model is pruned when
    B omits it); switching to a Model Provider Service clears the catalog (the header routes,
    models discovered at launch); and a workspace with no managed config also clears it. (The
    launch-time default pin is out of scope here; configure writes the catalog, not the config
    `model`.)
    """
    session = live_session

    set_managed_config_stub(session, tmp_path, None)
    _configure_unmanaged(session, workspace, "codex")
    assert _codex_listed(session) == []

    set_managed_config_stub(
        session,
        tmp_path,
        build_coding_agent_config("CODING_AGENT_CODEX", build_codex_agent_config(models=CODEX_A)),
    )
    _configure_managed(session, workspace)
    assert _codex_listed(session) == CODEX_A, _codex_listed(session)

    set_managed_config_stub(
        session,
        tmp_path,
        build_coding_agent_config("CODING_AGENT_CODEX", build_codex_agent_config(models=CODEX_B)),
    )
    _configure_managed(session, workspace)
    assert _codex_listed(session) == CODEX_B, _codex_listed(session)
    assert "system.ai.gpt-5-6-sol" not in _codex_listed(session)

    # Model discovery via MPS: the header routes, so the static catalog is cleared.
    set_managed_config_stub(
        session,
        tmp_path,
        build_coding_agent_config(
            "CODING_AGENT_CODEX", build_mps_agent_config("CODING_AGENT_CODEX", CODEX_MPS)
        ),
    )
    _configure_managed(session, workspace)
    assert _codex_listed(session) == []

    # Config gone: the catalog stays cleared, so an unmanaged workspace uses its own discovery.
    set_managed_config_stub(session, tmp_path, None)
    _configure_unmanaged(session, workspace, "codex")
    assert _codex_listed(session) == []
