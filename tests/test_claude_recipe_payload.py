"""Component coverage for the draft Claude Code recipe payload helpers."""

import json

import pytest

from ucode.smart_routing.recipe_payload import (
    CLAUDE_CODE_EXTRA_BODY_ENV_VAR,
    merge_claude_recipe_extra_body,
    update_claude_session_recipe,
)


def _extra_body(settings):
    return json.loads(settings["env"][CLAUDE_CODE_EXTRA_BODY_ENV_VAR])


def test_extra_body_merge_preserves_unrelated_settings_and_json_escaping():
    settings = {
        "theme": "dark",
        "env": {
            "KEEP_ENV": "yes",
            CLAUDE_CODE_EXTRA_BODY_ENV_VAR: json.dumps(
                {"metadata": {"source": "caller"}, "temperature": 0.2}
            ),
        },
    }

    assert merge_claude_recipe_extra_body(
        settings,
        True,
        {"SMART_ROUTER_NAME": 'custom<&"recipe'},
    )

    assert settings["theme"] == "dark"
    assert settings["env"]["KEEP_ENV"] == "yes"
    assert _extra_body(settings) == {
        "metadata": {"source": "caller"},
        "temperature": 0.2,
        "smart_router_recipe_name": 'custom<&"recipe',
    }


@pytest.mark.parametrize(
    "malformed",
    ["{not-json", "[]", 123],
)
def test_malformed_extra_body_is_not_destructively_overwritten(malformed):
    settings = {
        "theme": "dark",
        "env": {CLAUDE_CODE_EXTRA_BODY_ENV_VAR: malformed},
    }
    before = json.loads(json.dumps(settings))

    assert not merge_claude_recipe_extra_body(settings, True, {})
    assert settings == before


def test_atomic_session_update_supports_on_off_on_and_preserves_settings(tmp_path):
    watched = tmp_path / "claude-session-a" / "settings.json"
    watched.parent.mkdir()
    watched.write_text(
        json.dumps(
            {
                "permissions": {"deny": ["WebSearch"]},
                "env": {
                    "KEEP_ENV": "yes",
                    CLAUDE_CODE_EXTRA_BODY_ENV_VAR: json.dumps({"caller_field": "keep"}),
                },
            }
        ),
        encoding="utf-8",
    )

    expected = ["task_v3", None, "custom-v4"]
    controls = [
        (True, {}),
        (False, {}),
        (True, {"SMART_ROUTER_NAME": "custom-v4"}),
    ]
    for (enabled, env), recipe in zip(controls, expected, strict=True):
        assert update_claude_session_recipe(watched, enabled, env)
        settings = json.loads(watched.read_text(encoding="utf-8"))
        assert settings["permissions"] == {"deny": ["WebSearch"]}
        assert settings["env"]["KEEP_ENV"] == "yes"
        assert _extra_body(settings) == {
            "caller_field": "keep",
            "smart_router_recipe_name": recipe,
        }


def test_independent_session_files_do_not_share_recipe_state(tmp_path):
    first = tmp_path / "session-one.json"
    second = tmp_path / "session-two.json"
    original = {"env": {CLAUDE_CODE_EXTRA_BODY_ENV_VAR: json.dumps({"owner": "two"})}}
    first.write_text(json.dumps({"theme": "one"}), encoding="utf-8")
    second.write_text(json.dumps(original), encoding="utf-8")

    assert update_claude_session_recipe(first, False, {})

    assert _extra_body(json.loads(first.read_text(encoding="utf-8"))) == {
        "smart_router_recipe_name": None
    }
    assert json.loads(second.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("contents", ["not-json", "[]"])
def test_invalid_watched_settings_file_is_unchanged(tmp_path, contents):
    watched = tmp_path / "settings.json"
    watched.write_text(contents, encoding="utf-8")

    assert not update_claude_session_recipe(watched, True, {})
    assert watched.read_text(encoding="utf-8") == contents


def test_session_update_requires_an_existing_explicit_path(tmp_path):
    missing = tmp_path / "not-created.json"

    assert not update_claude_session_recipe(missing, True, {})
    assert not missing.exists()
