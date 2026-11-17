"""Auto metadata cleanup preserves unrelated settings and model restrictions."""

from copy import deepcopy

import pytest

from ucode.agents.claude_auto import auto_variant, remove_auto_configuration

SONNET = "system.ai.claude-sonnet-5-5"
HAIKU = "system.ai.claude-haiku-4-5"
ALIAS = auto_variant({"model": SONNET})


def test_removes_only_ug_metadata_without_mutating_input():
    settings = {
        "model": ALIAS,
        "modelOverrides": {ALIAS: SONNET, "team-model": HAIKU, "ug-auto": SONNET},
        "modelPicker": {
            "replaceBuiltInOptions": True,
            "customProperty": "keep",
            "options": [
                {"model": ALIAS, "label": "Auto"},
                {"model": "ug-auto", "label": "Legacy Auto"},
                {"model": SONNET, "label": "Sonnet"},
                {"model": "team-model", "label": "Auto"},
            ],
        },
        "availableModels": [SONNET, ALIAS, "ug-auto", "team-model"],
        "deniedModels": [HAIKU],
        "enforceAvailableModels": True,
        "env": {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": ALIAS,
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Auto",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Smart routing",
            "UNRELATED": ALIAS,
        },
        "permissions": {"deny": ["WebSearch"]},
    }
    original = deepcopy(settings)
    cleaned = remove_auto_configuration(settings)
    assert settings == original
    assert cleaned == {
        **original,
        "model": SONNET,
        "modelOverrides": {"team-model": HAIKU},
        "modelPicker": {
            **original["modelPicker"],
            "options": original["modelPicker"]["options"][2:],
        },
        "availableModels": [SONNET, "team-model"],
        "env": {"UNRELATED": ALIAS},
    }
    assert remove_auto_configuration(cleaned) == cleaned


@pytest.mark.parametrize("custom", ["team-auto", None])
def test_foreign_or_unattributed_custom_option_metadata_survives(custom):
    settings = {
        "env": {
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Auto",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "User description",
        }
    }
    if custom is not None:
        settings["env"]["ANTHROPIC_CUSTOM_MODEL_OPTION"] = custom
    assert remove_auto_configuration(settings) == settings


@pytest.mark.parametrize(
    "alias,target,mapping",
    [
        ("ug-auto", HAIKU, {"ug-auto": HAIKU}),
        (
            "ug-auto-old-deployment",
            "team.custom-model",
            {"ug-auto-old-deployment": "team.custom-model"},
        ),
        (ALIAS, SONNET, {}),
        (
            auto_variant({"model": SONNET + "[1m]"}).removesuffix("[1m]"),
            SONNET + "[1m]",
            {},
        ),
        (
            auto_variant(
                {"model": "team.custom-model", "behavesAs": "claude-sonnet-5-5[1m]"}
            ).removesuffix("[1m]"),
            "team.custom-model",
            {
                auto_variant(
                    {"model": "team.custom-model", "behavesAs": "claude-sonnet-5-5[1m]"}
                ): "team.custom-model"
            },
        ),
    ],
)
def test_saved_alias_resolves_only_from_mapping_or_verified_identity(alias, target, mapping):
    assert remove_auto_configuration({"model": alias}, alias_targets=mapping) == {"model": target}


def test_effective_mapping_wins_conflicting_local_mapping_and_digest():
    settings = {"model": ALIAS, "modelOverrides": {ALIAS: SONNET}}
    assert remove_auto_configuration(settings, alias_targets={ALIAS: HAIKU}) == {
        "model": HAIKU,
        "modelOverrides": {},
    }
    assert remove_auto_configuration(
        settings, alias_targets={ALIAS: HAIKU}, model_policy={"deniedModels": [HAIKU]}
    ) == {"modelOverrides": {}}


@pytest.mark.parametrize(
    "settings,policy",
    [
        ({"model": "ug-auto-unknown"}, None),
        ({"model": "ug-auto-000000000000-claude-sonnet-5-5"}, None),
        ({"model": ALIAS, "deniedModels": [SONNET]}, None),
        ({"model": ALIAS}, {"deniedModels": [SONNET]}),
        ({"model": ALIAS}, {"availableModels": [HAIKU]}),
        ({"model": ALIAS, "availableModels": [HAIKU, ALIAS]}, None),
    ],
)
def test_unresolved_or_disallowed_alias_selection_is_removed(settings, policy):
    cleaned = remove_auto_configuration(settings, model_policy=policy)
    assert "model" not in cleaned
    assert remove_auto_configuration(cleaned, model_policy=policy) == cleaned


@pytest.mark.parametrize(
    "key",
    [
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
    ],
)
def test_anthropic_model_selectors_are_repaired_without_changing_other_env(key):
    settings = {"env": {key: ALIAS, "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "My Sonnet"}}
    assert remove_auto_configuration(settings)["env"] == {
        key: SONNET,
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "My Sonnet",
    }
    settings["env"][key] = "ug-auto-unknown"
    assert remove_auto_configuration(settings)["env"] == {
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "My Sonnet"
    }


def test_alias_only_allowlist_becomes_bounded_real_targets():
    haiku_alias = auto_variant({"model": HAIKU})
    settings = {
        "model": ALIAS,
        "availableModels": [ALIAS, haiku_alias, ALIAS, "ug-auto-unknown"],
        "enforceAvailableModels": True,
        "deniedModels": ["system.ai.other"],
    }
    cleaned = remove_auto_configuration(settings)
    assert cleaned == {
        **settings,
        "model": SONNET,
        "availableModels": [SONNET, HAIKU],
    }
    assert remove_auto_configuration(cleaned) == cleaned


@pytest.mark.parametrize(
    "aliases,denied",
    [(["ug-auto-unknown"], []), ([ALIAS], [SONNET]), (["ug-auto"], [])],
)
def test_alias_only_allowlist_cannot_become_unrestricted(aliases, denied):
    settings = {"availableModels": aliases, "deniedModels": denied}
    original = deepcopy(settings)
    with pytest.raises(ValueError, match="Configure an explicit permitted real-model list"):
        remove_auto_configuration(settings)
    assert settings == original


def test_mixed_allowlist_drops_unknown_alias_without_adding_a_target():
    settings = {"availableModels": [HAIKU, "ug-auto-unknown", ALIAS]}
    assert remove_auto_configuration(settings) == {"availableModels": [HAIKU]}


@pytest.mark.parametrize(
    "settings",
    [{}, {"model": SONNET}, {"env": None, "modelPicker": []}, {"modelOverrides": "foreign"}],
)
def test_unrelated_documents_are_unchanged(settings):
    assert remove_auto_configuration(settings) == settings


def test_cleanup_does_not_depend_on_feature_environment(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_CONFIG_VERSION", "subagent_auto_v0_claude_only")
    monkeypatch.setenv("ENABLE_SMART_ROUTER_AUTO_MODE", "1")
    assert remove_auto_configuration({"model": ALIAS}) == {"model": SONNET}
