"""Fixture expectations owned by the catalog discovery journey, not shared utilities."""

MODEL_SCHEMA = "ug_e2e.models"
OTHER_MODEL_SCHEMA = "ug_e2e.other_models"
CLAUDE_DEFAULT = f"{MODEL_SCHEMA}.claude_sonnet"
CODEX_DEFAULT = f"{MODEL_SCHEMA}.gpt_luna"
GEMINI_MODEL = f"{MODEL_SCHEMA}.gemini_flash"
CLAUDE_MODELS = frozenset({CLAUDE_DEFAULT, f"{MODEL_SCHEMA}.claude_haiku", f"{MODEL_SCHEMA}.kimi"})
CODEX_MODELS = frozenset({CODEX_DEFAULT, f"{MODEL_SCHEMA}.kimi"})
MODEL_SERVICES = CLAUDE_MODELS | CODEX_MODELS | {GEMINI_MODEL}
CLAUDE_DECOY = f"{OTHER_MODEL_SCHEMA}.claude_decoy"
CODEX_DECOY = f"{OTHER_MODEL_SCHEMA}.codex_decoy"


def assert_model_policy(config: dict) -> None:
    """Require exact model policy while allowing unrelated resource fields."""
    assert config["spec_version"] == 1, config
    assert config["default_agent"] == "CODING_AGENT_CLAUDE_CODE", config
    entries = config["enabled_agents"]
    assert isinstance(entries, list) and len(entries) == 2, config
    assert all(isinstance(entry, dict) for entry in entries), entries
    by_agent = {entry.get("agent"): entry.get("config") for entry in entries}
    expected_defaults = {
        "CODING_AGENT_CLAUDE_CODE": {
            "default_model": CLAUDE_DEFAULT,
            "default_sonnet_model": CLAUDE_DEFAULT,
        },
        "CODING_AGENT_CODEX": {"default_model": CODEX_DEFAULT},
    }
    assert set(by_agent) == set(expected_defaults), by_agent
    for agent, defaults in expected_defaults.items():
        agent_config = by_agent[agent]
        assert isinstance(agent_config, dict), agent_config
        assert agent_config.get("models") == {"unity_catalog_location": MODEL_SCHEMA}, agent_config
        assert agent_config.get("default_models") == defaults, agent_config
        for key in ("smart_routing", "tracing"):
            settings = agent_config.get(key)
            assert isinstance(settings, dict) and settings.get("enabled") is False, agent_config
