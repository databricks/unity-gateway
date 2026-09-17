"""Managed-config CUJs driven by an injected admin config (UCODE_MANAGED_CONFIG_STUB).

These exercise the real installed `ug` against a real workspace, but the admin CodingAgentConfig
is injected locally so we can cover shapes the live workspace does not publish. Only the config
INPUT is stubbed; auth, normalization, config writers, and the agent CLIs stay real. Model ids are
the real ca-central model services so configure does not reject them. See tests/AGENTS.md rule 4.
"""

import json

import pytest

CLAUDE_MODELS = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
CODEX_MODEL = "system.ai.gpt-5-6-sol"


def _claude_agent(*, tracing: bool | None = None, family_defaults: bool = False) -> dict:
    default_models = {"default_model": CLAUDE_MODELS[0]}
    if family_defaults:
        default_models.update(
            default_opus_model=CLAUDE_MODELS[0],
            default_sonnet_model=CLAUDE_MODELS[1],
            default_haiku_model=CLAUDE_MODELS[2],
        )
    config = {"models": {"model_services": CLAUDE_MODELS}, "default_models": default_models}
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
    return session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)


def _workspace_state(session) -> dict:
    state = json.loads((session.home / ".ucode" / "state.json").read_text())
    return state["workspaces"][state["current_workspace"]]


def _settings_env(session) -> dict:
    settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    return settings.get("env") or {}


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_single_agent_claude_is_an_allowlist(live_session, workspace, tmp_path):
    """Scenario: a managed config enables only Claude, though both agent CLIs are installed.

    Expected: configure applies Claude alone; enabled_agents is an allowlist, not a hint.
    """
    session = live_session
    result = _apply(
        session, tmp_path, workspace, _config("CODING_AGENT_CLAUDE_CODE", _claude_agent())
    )
    assert "managed config is published" in result.stdout, result.stdout
    assert _workspace_state(session).get("available_tools") == ["claude"], _workspace_state(session)


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_managed_fixture_single_agent_codex_is_an_allowlist(live_session, workspace, tmp_path):
    """Scenario: a managed config enables only Codex, though both agent CLIs are installed.

    Expected: configure applies Codex alone.
    """
    session = live_session
    result = _apply(session, tmp_path, workspace, _config("CODING_AGENT_CODEX", _codex_agent()))
    assert "managed config is published" in result.stdout, result.stdout
    assert _workspace_state(session).get("available_tools") == ["codex"], _workspace_state(session)


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_tracing_enabled_writes_otel(live_session, workspace, tmp_path):
    """Scenario: a managed config enables tracing for Claude.

    Expected: configure writes the OTLP telemetry env pointing at the workspace gateway endpoint.
    """
    session = live_session
    _apply(
        session,
        tmp_path,
        workspace,
        _config("CODING_AGENT_CLAUDE_CODE", _claude_agent(tracing=True)),
    )
    env = _settings_env(session)
    assert env.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1", env
    assert env.get("OTEL_TRACES_EXPORTER") == "otlp", env
    assert "/ai-gateway/otel/v1/traces" in env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""), env


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_tracing_absent_writes_no_otel(live_session, workspace, tmp_path):
    """Scenario: a managed config does not enable tracing.

    Expected: configure writes none of the OTLP telemetry env keys.
    """
    session = live_session
    _apply(session, tmp_path, workspace, _config("CODING_AGENT_CLAUDE_CODE", _claude_agent()))
    env = _settings_env(session)
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in env, env
    assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" not in env, env


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_family_default_models_write_env(live_session, workspace, tmp_path):
    """Scenario: a managed config pins per-family default models for Claude.

    Expected: configure writes each family default into its ANTHROPIC_DEFAULT_*_MODEL env key.
    """
    session = live_session
    config = _config("CODING_AGENT_CLAUDE_CODE", _claude_agent(family_defaults=True))
    _apply(session, tmp_path, workspace, config)
    env = _settings_env(session)

    # ug may append the "[1m]" 1M-context suffix to a family default; compare the base model id.
    def base(model: str | None) -> str | None:
        return model[: -len("[1m]")] if isinstance(model, str) and model.endswith("[1m]") else model

    assert base(env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")) == CLAUDE_MODELS[0], env
    assert base(env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")) == CLAUDE_MODELS[1], env
    assert base(env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")) == CLAUDE_MODELS[2], env
