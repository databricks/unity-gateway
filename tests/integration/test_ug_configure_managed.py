"""CUJs: configure against a managed workspace, where an admin publishes the setup.

These run against the managed e2e workspace (`E2E_ADMIN_WORKSPACE`), which publishes a
CodingAgentConfig. They are the only journeys that exercise the managed config fetch end to end:
`ug configure` applies the admin config to every enabled agent without the personal agent
selector, and each agent's generated config exposes exactly the admin's static
`model_services` (Claude's `availableModels`/`modelPicker`, Codex's model catalog). The
expected model ids mirror the published config; update them here if the admin list changes.
"""

import json

import pytest
from utils.constants import MANAGED_CLAUDE_MODELS, MANAGED_CODEX_MODELS
from utils.terminal import AgentTerminal


@pytest.mark.managed
@pytest.mark.claude
def test_ug_configure_managed_claude(live_session, workspace):
    """Scenario: run `ug configure` on a workspace that publishes a managed config.

    Expected: ug applies the admin config to every enabled agent without showing the
    personal agent selector, Claude's generated settings expose exactly the admin's static
    model_services as its picker allow-list, and launching Claude reaches a real gateway
    prompt rather than the account-login flow.
    """
    session = live_session
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    assert settings.get("availableModels") == MANAGED_CLAUDE_MODELS, settings
    options = (settings.get("modelPicker") or {}).get("options", [])
    assert [option.get("model") for option in options] == MANAGED_CLAUDE_MODELS, settings

    # Drive the real agent: with the managed models in place it must reach a usable
    # gateway prompt, not Claude's own login flow (boot asserts the latter never appears).
    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "managed-claude") as tui:
        tui.boot()
        tui.check_input_and_exit()


@pytest.mark.managed
@pytest.mark.codex
def test_ug_configure_managed_codex(live_session, workspace):
    """Scenario: run `ug configure` on a workspace that publishes a managed config.

    Expected: ug applies the admin config to every enabled agent without showing the
    personal agent selector, Codex's generated model catalog lists exactly the admin's static
    model_services, and launching Codex reaches a real gateway prompt rather than the
    account-login flow.
    """
    session = live_session
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    catalog = json.loads((session.home / ".ucode" / "codex-model-catalog.json").read_text())
    listed = [
        model.get("slug")
        for model in catalog.get("models", [])
        if model.get("visibility") == "list"
    ]
    assert listed == MANAGED_CODEX_MODELS, catalog

    with AgentTerminal(session, "codex", [str(session.binary), "codex"], "managed-codex") as tui:
        tui.boot()
        tui.check_input_and_exit()


@pytest.mark.managed
@pytest.mark.claude
def test_ug_configure_managed_is_idempotent(live_session, workspace):
    """Scenario: run the managed `ug configure` twice in the same session.

    Expected: each run, with no personal agent selector, applies the admin config to both enabled
    agents identically, so a repeat configure neither duplicates, drops, nor rewrites any entry.
    """
    session = live_session
    runs = []
    for _ in range(2):
        result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
        assert "Select coding agents to configure:" not in result.stdout, result.stdout
        settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
        catalog = json.loads((session.home / ".ucode" / "codex-model-catalog.json").read_text())
        picker = [o.get("model") for o in (settings.get("modelPicker") or {}).get("options", [])]
        listed = [m.get("slug") for m in catalog.get("models", []) if m.get("visibility") == "list"]
        runs.append((settings.get("availableModels"), picker, listed))

    expected = (MANAGED_CLAUDE_MODELS, MANAGED_CLAUDE_MODELS, MANAGED_CODEX_MODELS)
    assert runs == [expected, expected], runs


@pytest.mark.managed
@pytest.mark.claude
def test_ug_managed_config_launch_reuses_cache_within_ttl(live_session, workspace):
    """Scenario: after a managed `ug configure`, launch Claude within the cache TTL, then again
    after the cached read is backdated past it.

    Expected: configure stamps managed-config.json with a `published` outcome and a `retrieved_at`;
    a launch within the TTL is served from that cache and leaves the stamp untouched (no
    control-plane re-read), and once the stamp is backdated past the TTL the next launch reads fresh
    and advances it.
    """
    session = live_session
    cache = session.home / ".ucode" / "managed-config.json"

    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    fresh = json.loads(cache.read_text())
    assert fresh["outcome"] == "published", fresh
    stamped = fresh["retrieved_at"]

    # A launch within the TTL reuses the cached read: the stamp must not move.
    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "ttl-cache-hit") as tui:
        tui.boot()
        tui.check_input_and_exit()
    assert json.loads(cache.read_text())["retrieved_at"] == stamped

    # Backdate the stamp past the TTL; the next launch reads fresh and advances it.
    backdated = "2000-01-01T00:00:00+00:00"
    cache.write_text(json.dumps({**json.loads(cache.read_text()), "retrieved_at": backdated}))
    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "ttl-expired") as tui:
        tui.boot()
        tui.check_input_and_exit()
    assert json.loads(cache.read_text())["retrieved_at"] != backdated
