"""Managed-config CUJs driven by an injected admin config (UCODE_MANAGED_CONFIG_STUB).

These exercise the real installed `ug configure` against a real workspace, but the admin
CodingAgentConfig is injected locally so we can cover shapes the live workspace does not publish.
Only the config INPUT is stubbed; auth, normalization, and the config writers stay real. See
tests/AGENTS.md rule 4.

Each case deliberately differs from ca-central's published config in the dimension it asserts
(agent count, model list, or tracing), so a pass proves the injected config drove configure rather
than a live fetch. Model ids are real ca-central model services so configure does not reject them.
"""

import json

import pytest

CLAUDE_MODELS = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
CODEX_MODEL = "system.ai.gpt-5-6-sol"


def _claude_agent(models: list[str], *, tracing: bool | None = None) -> dict:
    config = {
        "models": {"model_services": models},
        "default_models": {"default_model": models[0]},
    }
    if tracing is not None:
        config["tracing"] = {"enabled": tracing}
    return {"agent": "CODING_AGENT_CLAUDE_CODE", "config": config}


def _codex_agent() -> dict:
    return {
        "agent": "CODING_AGENT_CODEX",
        "config": {
            "models": {"model_services": [CODEX_MODEL]},
            "default_models": {"default_model": CODEX_MODEL},
        },
    }


def _config(default_agent: str, *agents: dict) -> dict:
    return {"spec_version": 1, "default_agent": default_agent, "enabled_agents": list(agents)}


def _apply(session, tmp_path, workspace, config: dict):
    stub = tmp_path / "managed-config.json"
    stub.write_text(json.dumps(config))
    session.env["UCODE_MANAGED_CONFIG_STUB"] = str(stub)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "managed config is published" in result.stdout, result.stdout
    return result


def _workspace_state(session) -> dict:
    state = json.loads((session.home / ".ucode" / "state.json").read_text())
    return state["workspaces"][state["current_workspace"]]


def _claude_settings(session) -> dict:
    return json.loads((session.home / ".claude" / "ucode-settings.json").read_text())


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_single_agent_claude_is_an_allowlist(live_session, workspace, tmp_path):
    """Scenario: an injected config enables only Claude, though both agent CLIs are installed.

    Expected: configure applies Claude alone. enabled_agents is an allowlist, and ["claude"] can
    only come from the injected config (the live workspace enables both agents).
    """
    session = live_session
    _apply(
        session,
        tmp_path,
        workspace,
        _config("CODING_AGENT_CLAUDE_CODE", _claude_agent(CLAUDE_MODELS)),
    )
    assert _workspace_state(session).get("available_tools") == ["claude"], _workspace_state(session)


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_managed_fixture_single_agent_codex_is_an_allowlist(live_session, workspace, tmp_path):
    """Scenario: an injected config enables only Codex, though both agent CLIs are installed.

    Expected: configure applies Codex alone (the live workspace enables both agents).
    """
    session = live_session
    _apply(session, tmp_path, workspace, _config("CODING_AGENT_CODEX", _codex_agent()))
    assert _workspace_state(session).get("available_tools") == ["codex"], _workspace_state(session)


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_static_model_list_comes_from_the_config(live_session, workspace, tmp_path):
    """Scenario: an injected config pins a Claude model list distinct from the live workspace's.

    Expected: Claude's availableModels and picker equal exactly the injected two-model list, proving
    the model allow-list is driven by the config (the live workspace publishes three Claude models).
    """
    session = live_session
    models = [CLAUDE_MODELS[0], CLAUDE_MODELS[2]]
    _apply(session, tmp_path, workspace, _config("CODING_AGENT_CLAUDE_CODE", _claude_agent(models)))

    settings = _claude_settings(session)
    assert settings.get("availableModels") == models, settings
    options = (settings.get("modelPicker") or {}).get("options", [])
    assert [option.get("model") for option in options] == models, settings


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_tracing_enabled_writes_otel(live_session, workspace, tmp_path):
    """Scenario: an injected config enables tracing for Claude (the live workspace does not).

    Expected: configure writes the OTLP telemetry env pointing at the workspace gateway endpoint.
    """
    session = live_session
    config = _config("CODING_AGENT_CLAUDE_CODE", _claude_agent(CLAUDE_MODELS, tracing=True))
    _apply(session, tmp_path, workspace, config)

    env = _claude_settings(session).get("env") or {}
    assert env.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1", env
    assert env.get("OTEL_TRACES_EXPORTER") == "otlp", env
    assert (
        env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") == f"{workspace}/ai-gateway/otel/v1/traces"
    ), env
