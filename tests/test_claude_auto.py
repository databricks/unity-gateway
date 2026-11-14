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


def test_auto_uses_configured_default_and_preserves_catalog():
    catalog = AnthropicModelCatalog(
        ["system.ai.claude-fable-5-1", "system.ai.claude-opus-5-5[1m]", AUTO_MODEL],
        {"system.ai.claude-opus-5-5[1m]": "Opus"},
    )
    result = json.loads(
        picker_config(catalog, "https://workspace", default_model="system.ai.claude-opus-5-5[1m]")
    )
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
@pytest.mark.parametrize(
    "managed_default,missing_alias", [(None, False), ("admin-default", False), (None, True)]
)
def test_native_launch_default_precedence_and_compatibility(
    tmp_path,
    monkeypatch,
    saved,
    pinned,
    forwarded,
    installed,
    feedback,
    expected,
    has_auto,
    managed_default,
    missing_alias,
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
    monkeypatch.setenv("ENABLE_SMART_ROUTER_AUTO_MODE", "1")
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", "1" if feedback else "0")
    monkeypatch.setenv(v2.SESSION_PYTHON_ENV_VAR, sys.executable)
    monkeypatch.setattr(v2, "APP_DIR", tmp_path)
    monkeypatch.setattr(claude, "auto_picker_available", lambda: installed)
    managed_path = tmp_path / "managed.json"
    if managed_default or missing_alias:
        managed_settings = add_picker_entry(
            {
                "modelPicker": {
                    "options": [
                        {"model": "other" if missing_alias else "system.ai.claude-opus-5-5"}
                    ]
                },
                "env": {"ANTHROPIC_MODEL": managed_default} if managed_default else {},
            }
        )
        managed_path.write_text(json.dumps(managed_settings))
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_path)
    if missing_alias:
        has_auto = False
    if (managed_default or missing_alias) and not pinned and "--model" not in forwarded:
        expected = None
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


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("foreign_picker", [False, True])
def test_configure_changes_only_ug_owned_picker_when_enabled(
    tmp_path, monkeypatch, enabled, foreign_picker
):
    from tests.test_claude_header_mod import WS, _managed_env
    from ucode.agents import claude

    existing = (
        {"modelPicker": {"replaceBuiltInOptions": True, "options": [{"model": "admin-model"}]}}
        if foreign_picker
        else None
    )
    path, _, writes = _managed_env(tmp_path, monkeypatch, existing)
    monkeypatch.setenv("ENABLE_SMART_ROUTER_AUTO_MODE", "1" if enabled else "0")
    monkeypatch.setattr(claude, "_explicit_configure", True)
    state = {"workspace": WS, "claude_models": {"opus": "opus"}}
    if not foreign_picker:
        state["claude_static_models"] = ["opus"]
    claude.write_tool_config(state, "opus")
    configured = json.loads(path.read_text())
    assert picker_is_configured(configured) is (enabled and not foreign_picker)
    if foreign_picker:
        assert configured["modelPicker"] == existing["modelPicker"]
    before = path.read_bytes()
    count = len(writes)
    monkeypatch.setattr(claude, "_explicit_configure", False)
    claude.write_tool_config(state, "opus")
    assert path.read_bytes() == before
    assert len(writes) == count


def test_personal_configure_does_not_discover_or_own_picker(tmp_path, monkeypatch):
    from tests.test_claude_header_mod import WS, _managed_env
    from ucode.agents import claude

    path, _, _ = _managed_env(tmp_path, monkeypatch)
    monkeypatch.setenv("ENABLE_SMART_ROUTER_AUTO_MODE", "1")
    monkeypatch.setattr(claude, "_explicit_configure", True)
    monkeypatch.setattr(
        "ucode.databricks.list_anthropic_model_catalog", lambda *_: pytest.fail("Extra discovery")
    )
    claude.write_tool_config({"workspace": WS, "claude_models": {"opus": "opus"}}, "opus")
    assert "modelPicker" not in json.loads(path.read_text())


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


@pytest.mark.parametrize("value", [None, [], "malformed", 7])
@pytest.mark.parametrize("key", ["modelPicker", "modelOverrides"])
def test_malformed_settings_do_not_enable_auto(key, value):
    assert not picker_is_configured({key: value})


def test_admin_custom_option_is_preserved():
    custom = {
        "ANTHROPIC_CUSTOM_MODEL_OPTION": "admin-model",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Admin",
    }
    original = {"env": custom, "modelPicker": {"options": [{"model": "opus"}]}}
    assert add_picker_entry(original)["env"] == custom


@pytest.mark.parametrize("rows", [[], [None, {}, {"model": "ug-auto"}]])
def test_empty_or_malformed_picker_falls_through(tmp_path, monkeypatch, rows):
    from ucode.agents import claude

    path = tmp_path / "managed.json"
    path.write_text(json.dumps({"modelPicker": {"options": rows}}))
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: path)
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", tmp_path / "absent")
    monkeypatch.setattr(claude, "CLAUDE_USER_SETTINGS_PATH", tmp_path / "absent")
    assert v2._model_picker_catalog() is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("modelPicker", None),
        ("modelPicker", []),
        ("modelOverrides", []),
        ("modelOverrides", None),
        ("env", []),
    ],
)
def test_malformed_optional_auto_settings_do_not_crash(field, value):
    settings = {"modelPicker": {"options": [{"model": "opus"}]}, field: value}
    assert not picker_is_configured(settings)
    assert add_picker_entry(settings) == settings


def test_admin_custom_option_and_hidden_builtins_are_preserved():
    base = {
        "modelPicker": {"options": [{"model": "opus"}], "replaceBuiltInOptions": True},
        "env": {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "admin-model",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Admin",
        },
    }
    assert add_picker_entry(base) == base
    assert not picker_is_configured(base)
    base.pop("env")
    assert add_picker_entry(base, owns_picker=False) == base
    assert picker_is_configured(add_picker_entry(base, owns_picker=True))


def test_only_ug_custom_option_is_migrated():
    settings = {
        "modelPicker": {"options": [{"model": "opus"}]},
        "env": {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "ug-auto",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Auto",
            "OTHER": "keep",
        },
    }
    assert add_picker_entry(settings)["env"] == {"OTHER": "keep"}


def test_managed_default_wins_and_absent_default_uses_catalog_order():
    settings = add_picker_entry(
        {
            "modelPicker": {"options": [{"model": "sonnet"}, {"model": "opus"}]},
            "env": {"ANTHROPIC_MODEL": "sonnet"},
        }
    )
    catalog = AnthropicModelCatalog(["sonnet", "opus"], {})
    assert (
        json.loads(picker_config(catalog, "w", settings, default_model="opus"))["mainModel"]
        == "sonnet"
    )
    assert json.loads(picker_config(catalog, "w"))["mainModel"] == "sonnet"


@pytest.mark.parametrize("rows", [[], [None, {"model": "ug-auto"}]])
def test_empty_picker_can_fall_through_without_bypassing_denies(tmp_path, monkeypatch, rows):
    from ucode.agents import claude

    managed, user = tmp_path / "managed.json", tmp_path / "user.json"
    managed.write_text(json.dumps({"modelPicker": {"options": rows}, "deniedModels": ["opus"]}))
    user.write_text(
        json.dumps({"modelPicker": {"options": [{"model": "opus"}, {"model": "sonnet"}]}})
    )
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed)
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", user)
    monkeypatch.setattr(claude, "CLAUDE_USER_SETTINGS_PATH", tmp_path / "absent")
    assert v2._model_picker_catalog().model_ids == ["sonnet"]


def test_configure_preserves_admin_custom_slot_and_does_not_claim_it(tmp_path, monkeypatch):
    from tests.test_claude_header_mod import WS, _managed_env
    from ucode.agents import claude

    admin = {
        "ANTHROPIC_CUSTOM_MODEL_OPTION": "team-custom",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Team custom",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Admin-owned option",
    }
    original = {
        "env": admin,
        "modelPicker": {"options": [{"model": "opus"}], "replaceBuiltInOptions": True},
    }
    path, _, _ = _managed_env(tmp_path, monkeypatch, original)
    monkeypatch.setenv("ENABLE_SMART_ROUTER_AUTO_MODE", "1")
    monkeypatch.setattr(claude, "_explicit_configure", True)
    claude.write_tool_config({"workspace": WS, "claude_models": {"opus": "opus"}}, "opus")
    result = json.loads(path.read_text())
    assert {key: result["env"][key] for key in admin} == admin
    assert result["modelPicker"] == original["modelPicker"]
    assert not claude.auto_picker_available()
