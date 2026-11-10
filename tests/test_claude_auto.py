"""Launch catalog and model identity for the native Claude Auto picker."""

import json
import sys
from pathlib import Path

import pytest

from ucode.agents.claude_auto import (
    AUTO_MODEL,
    add_picker_entry,
    auto_variant,
    picker_config,
    picker_is_configured,
)
from ucode.databricks import AnthropicModelCatalog
from ucode.smart_routing import v2


def test_auto_is_distinct_and_opus_55_is_default():
    catalog = AnthropicModelCatalog(
        ["system.ai.claude-fable-5-1", "system.ai.claude-opus-5-5[1m]", AUTO_MODEL],
        {"system.ai.claude-opus-5-5[1m]": "Opus"},
    )
    result = json.loads(picker_config(catalog, "https://workspace"))
    assert result["model"] == auto_variant({"model": "system.ai.claude-opus-5-5[1m]"})
    assert result["mainModel"] == "system.ai.claude-opus-5-5[1m]"
    assert result["models"] == [
        {
            "model": "system.ai.claude-fable-5-1",
            "label": "Fable 5.1",
            "autoModel": auto_variant({"model": "system.ai.claude-fable-5-1"}),
        },
        {
            "model": "system.ai.claude-opus-5-5[1m]",
            "label": "Opus 5.5",
            "autoModel": auto_variant({"model": "system.ai.claude-opus-5-5[1m]"}),
        },
    ]
    other = json.loads(picker_config(catalog, "https://other-workspace"))
    assert result["storeKey"] != other["storeKey"]


def test_default_stays_in_available_catalog_and_preserves_custom_labels():
    result = json.loads(
        picker_config(AnthropicModelCatalog(["custom"], {"custom": "Team model"}), "w")
    )
    assert result["mainModel"] == "custom"
    assert result["models"] == [
        {"model": "custom", "label": "Team model", "autoModel": auto_variant({"model": "custom"})}
    ]
    with pytest.raises(ValueError, match="at least one"):
        picker_config(AnthropicModelCatalog([AUTO_MODEL], {}), "w")


@pytest.mark.parametrize(
    ("saved", "pinned", "forwarded", "installed", "feedback", "expected", "has_auto"),
    [
        (None, None, [], True, False, AUTO_MODEL, True),
        (AUTO_MODEL, None, [], True, False, AUTO_MODEL, True),
        ("opus", None, [], True, False, "opus", True),
        (AUTO_MODEL, "opus", [], True, False, "opus", True),
        (None, None, ["--model", "opus"], True, False, "opus", True),
        (None, None, [], False, False, None, False),
        (None, None, [], True, True, None, False),
        (None, None, ["-p", "hello"], True, False, None, False),
        ("opus", None, ["--resume", "session-id"], True, False, None, True),
        ("opus", None, ["--continue"], True, False, None, True),
        (None, None, ["--resume=session-id"], True, False, None, True),
        (None, "opus", ["--continue"], True, False, "opus", True),
    ],
)
def test_native_launch_default_precedence_and_compatibility(
    tmp_path, monkeypatch, saved, pinned, forwarded, installed, feedback, expected, has_auto
):
    from ucode import custom_oauth
    from ucode.agents import claude

    for key in (
        "ANTHROPIC_CUSTOM_MODEL_OPTION",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "0")
    monkeypatch.setenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, "1")
    monkeypatch.setenv("ENABLE_SMART_ROUTER_ORCHESTRATOR", "0")
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", "1" if feedback else "0")
    monkeypatch.setenv(v2.SESSION_PYTHON_ENV_VAR, sys.executable)
    monkeypatch.setattr(v2, "APP_DIR", tmp_path)
    monkeypatch.setattr(claude, "auto_picker_available", lambda: installed)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: tmp_path / "absent-managed.json")
    monkeypatch.setattr(
        v2,
        "_model_picker_catalog",
        lambda: AnthropicModelCatalog(["system.ai.claude-opus-5-5"], {}),
    )
    monkeypatch.setattr(custom_oauth, "get_databricks_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(v2, "build_auth_token_argv", lambda *_args, **_kwargs: ["ug"])
    monkeypatch.setattr("ucode.databricks.ug_binary", lambda: "/bin/ug")
    monkeypatch.setattr(v2, "_prepare_smart_router_session", lambda _agent: tmp_path / "env.json")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text(json.dumps({"model": saved, "theme": "dark"}))
    original = user_settings.read_bytes()
    captured = {}

    class Process:
        def __init__(self, argv, **_kwargs):
            captured["argv"] = argv
            captured["settings"] = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())

        def wait(self):
            return 0

    monkeypatch.setattr(v2.subprocess_cross_os, "popen", Process)
    with pytest.raises(SystemExit) as exit_info:
        v2.launch_claude(
            {"workspace": "https://example.com"},
            forwarded,
            binary="claude",
            user_settings_path=user_settings,
            launch_model=pinned,
            compose_settings=lambda args: ({}, args),
            launch_model_args=claude._launch_model_args,
            model_name=claude._maybe_add_1m_suffix,
        )
    assert exit_info.value.code == 0
    if expected == AUTO_MODEL:
        expected = auto_variant({"model": "system.ai.claude-opus-5-5"})
    argv = captured["argv"]
    assert (argv[argv.index("--model") + 1] if "--model" in argv else None) == expected
    options = captured["settings"]["pluginConfigs"]["ug-smart-router"]["options"]
    assert ("autoPicker" in options) is has_auto
    if has_auto:
        assert picker_is_configured(captured["settings"])
        assert json.loads(options["autoPicker"])["mainModel"] == "system.ai.claude-opus-5-5"
        assert options["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] == "1"
    assert user_settings.read_bytes() == original


def test_auto_configured_in_actual_picker_and_allowlist_without_widening_targets():
    original = {
        "availableModels": ["system.ai.claude-opus-5"],
        "enforceAvailableModels": True,
        "modelPicker": {
            "replaceBuiltInOptions": True,
            "options": [
                {"model": "system.ai.claude-opus-5", "label": "Opus 5"},
                {"model": "system.ai.claude-opus-5-5", "label": "Opus 5.5"},
            ],
        },
        "modelOverrides": {"other": "custom"},
        "prependPlugins": ["sec-default@builtin"],
    }
    result = add_picker_entry(original)
    alias = auto_variant({"model": "system.ai.claude-opus-5"})
    assert result["availableModels"] == ["system.ai.claude-opus-5", alias]
    assert result["modelOverrides"] == {"other": "custom", alias: "system.ai.claude-opus-5"}
    assert result["modelPicker"]["replaceBuiltInOptions"] is False
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in result["env"]
    assert result["modelPicker"]["options"] == original["modelPicker"]["options"]
    assert result["enforceAvailableModels"] is True
    assert result["prependPlugins"] == original["prependPlugins"]
    assert original["availableModels"] == ["system.ai.claude-opus-5"]
    assert picker_is_configured(result)
    assert add_picker_entry(result) == result
    assert not picker_is_configured(original)
    blocked = {**original, "deniedModels": ["system.ai.claude-opus-5"]}
    assert not picker_is_configured(add_picker_entry(blocked))
    assert add_picker_entry(blocked)["availableModels"] == blocked["availableModels"]


@pytest.mark.parametrize(
    ("main", "behavior"),
    [
        ({"model": "system.ai.claude-opus-5-5"}, "claude-opus-5-5"),
        ({"model": "system.ai.claude-sonnet-5-5[1m]"}, "claude-sonnet-5-5[1m]"),
        ({"model": "custom-deployment", "behavesAs": "claude-haiku-5-5"}, "claude-haiku-5-5"),
        ({"model": "gpt-6", "label": "Claude Opus 5.5"}, None),
    ],
)
def test_auto_inherits_fallback_capabilities_without_guessing_from_labels(main, behavior):
    settings = {"modelPicker": {"options": [main]}}
    result = add_picker_entry(settings)
    alias = auto_variant(main)
    assert alias.endswith(behavior or main["model"])
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in result["env"]
    assert result["modelOverrides"][alias] == main["model"]
    assert settings["modelPicker"]["options"] == [main]
    assert add_picker_entry(result) == result


@pytest.mark.parametrize("managed_picker", [False, True])
def test_managed_policy_cannot_fall_through_to_unrestricted_user_models(
    tmp_path, monkeypatch, managed_picker
):
    from ucode.agents import claude

    managed = tmp_path / "managed.json"
    user = tmp_path / "user.json"
    policy = {"availableModels": ["opus"], "deniedModels": ["opus"]}
    rows = {"options": [{"model": "opus"}, {"model": "astra"}]}
    if managed_picker:
        policy["modelPicker"] = rows
    managed.write_text(json.dumps(policy))
    user.write_text(json.dumps({"availableModels": [], "modelPicker": rows}))
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed)
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(claude, "CLAUDE_USER_SETTINGS_PATH", user)

    catalog = v2._model_picker_catalog()
    assert catalog is not None  # None would rediscover an unrestricted gateway list.
    assert catalog.model_ids == []
    assert "No permitted models" in catalog.error_msg

    policy["deniedModels"] = []
    managed.write_text(json.dumps(policy))
    assert v2._model_picker_catalog().model_ids == ["opus"]


@pytest.mark.parametrize("existing_picker", [False, True])
def test_configure_installs_auto_before_native_startup_and_launch_does_not_write(
    tmp_path, monkeypatch, existing_picker
):
    from tests.test_claude_header_mod import WS, _managed_env
    from ucode.agents import claude

    existing = (
        {
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [{"model": "system.ai.claude-opus-5", "label": "Opus 5"}],
            }
        }
        if existing_picker
        else None
    )
    path, _root, writes = _managed_env(tmp_path, monkeypatch, existing)
    state = {
        "workspace": WS,
        "claude_static_models": ["system.ai.claude-opus-5"],
        "claude_models": {"opus": "system.ai.claude-opus-5"},
    }
    if existing_picker:
        # No central CodingAgentConfig: keep the existing machine-wide list.
        state.pop("claude_static_models")
    monkeypatch.setattr(claude, "_explicit_configure", False)
    claude.write_tool_config(state, "system.ai.claude-opus-5")
    assert not claude.auto_picker_available()
    assert not picker_is_configured(json.loads(path.read_text()))
    monkeypatch.setattr(claude, "_explicit_configure", True)
    claude.write_tool_config(state, "system.ai.claude-opus-5")
    assert claude.auto_picker_available()
    assert picker_is_configured(json.loads(path.read_text()))
    before = path.read_bytes()
    count = len(writes)
    monkeypatch.setattr(claude, "_explicit_configure", False)
    claude.write_tool_config(state, "system.ai.claude-opus-5")
    assert path.read_bytes() == before
    assert len(writes) == count


def test_configure_discovers_catalog_without_central_config(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.test_claude_header_mod import WS, _managed_env
    from ucode.agents import claude

    path, _, writes = _managed_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        claude, "refresh_managed_config", lambda *_a, **_k: SimpleNamespace(manifest=None)
    )
    monkeypatch.setattr(claude, "get_databricks_token", lambda *_a: "fixture")
    catalog = AnthropicModelCatalog(["system.ai.claude-opus-5-5", "astra"], {"astra": "Astra"})
    monkeypatch.setattr("ucode.databricks.list_anthropic_model_catalog", lambda *_a: catalog)
    state = {"workspace": WS, "claude_models": {"opus": "system.ai.claude-opus-5-5"}}
    claude.write_tool_config(state, "system.ai.claude-opus-5-5", derive_managed_model_inputs=True)
    assert picker_is_configured(json.loads(path.read_text()))
    before = path.read_bytes()
    count = len(writes)
    monkeypatch.setattr(claude, "_explicit_configure", False)
    claude.write_tool_config(
        state,
        "system.ai.claude-opus-5-5",
        picker_catalog=claude.default_model_picker_catalog(
            state["claude_models"], discovered_catalog=catalog
        ),
    )
    assert path.read_bytes() == before
    assert len(writes) == count


def test_auto_profiles_are_immutable_and_migrate_stale_aliases():
    opus = {"model": "system.ai.claude-opus-5-5"}
    haiku = {"model": "team-fast", "behavesAs": "claude-haiku-4-5"}
    original = {
        "modelPicker": {
            "replaceBuiltInOptions": True,
            "options": [{"model": AUTO_MODEL, "label": "Auto"}, opus, haiku],
        },
        "availableModels": [AUTO_MODEL, opus["model"], haiku["model"]],
        "modelOverrides": {AUTO_MODEL: opus["model"], "other": "unrelated"},
    }
    result = add_picker_entry(original)
    assert result["modelPicker"]["options"] == [opus, haiku]
    assert result["modelOverrides"] == {
        auto_variant(opus): opus["model"],
        auto_variant(haiku): haiku["model"],
        "other": "unrelated",
    }
    config = json.loads(
        picker_config(AnthropicModelCatalog([opus["model"], haiku["model"]], {}), "w", result)
    )
    assert config["models"][1]["autoModel"] == auto_variant(haiku)
    assert add_picker_entry(result) == result
    withdrawn = {**result, "deniedModels": [haiku["model"]]}
    cleaned = add_picker_entry(withdrawn)
    assert auto_variant(haiku) not in cleaned["modelOverrides"]
    assert auto_variant(haiku) not in cleaned["availableModels"]
    assert cleaned["modelOverrides"][auto_variant(opus)] == opus["model"]


def test_separate_deployments_with_same_profile_have_distinct_aliases():
    a = {"model": "a", "behavesAs": "claude-sonnet-5-5[1m]"}
    b = {"model": "b", "behavesAs": a["behavesAs"]}
    assert auto_variant(a) != auto_variant(b)
    assert auto_variant(a).endswith(a["behavesAs"])


def test_removing_last_auto_alias_does_not_turn_allowlist_into_unrestricted():
    original = {
        "availableModels": [AUTO_MODEL],
        "modelOverrides": {AUTO_MODEL: "opus"},
        "modelPicker": {"options": [{"model": AUTO_MODEL}, {"model": "opus"}]},
    }
    result = add_picker_entry(original)
    assert result["availableModels"] == [AUTO_MODEL]
    assert AUTO_MODEL not in result["modelOverrides"]
    assert not picker_is_configured(result)
