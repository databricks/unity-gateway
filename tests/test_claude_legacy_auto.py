"""Claude 2.1.236 terminal Auto state, transcript boundaries and launch helpers."""

import json
import os
import sys
from pathlib import Path

import pytest

from ucode import custom_oauth
from ucode.agents import claude
from ucode.agents.claude_auto import add_picker_entry
from ucode.config_io import read_json_safe
from ucode.smart_routing import claude_hooks, v2
from ucode.smart_routing import claude_legacy_auto as auto


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, "APP_DIR", tmp_path)
    return {
        "model": "ug-auto-sonnet",
        "mainModel": "claude-sonnet-4-6",
        "storeKey": "workspace-preference",
        "models": [
            {"model": "claude-sonnet-4-6", "label": "Sonnet 4.6", "autoModel": "ug-auto-sonnet"},
            {"model": "claude-haiku-4-5", "label": "Haiku 4.5", "autoModel": "ug-auto-haiku"},
        ],
    }


@pytest.fixture
def selection(catalog, tmp_path):
    return auto.AutoSelection(
        catalog,
        catalog["mainModel"],
        True,
        tmp_path / "controls.json",
        {"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"},
    )


def test_preference_respects_catalog_explicit_models_and_resume(catalog):
    path = auto.preference_path(catalog)
    path.parent.mkdir()
    path.write_text('{"model":"claude-haiku-4-5"}')
    assert auto.initial_model(catalog, None, resume=False) == ("claude-haiku-4-5", True)
    assert auto.initial_model(catalog, "claude-sonnet-4-6", resume=False) == (
        "claude-sonnet-4-6",
        False,
    )
    assert auto.initial_model(catalog, None, resume=True) == (None, False)
    path.write_text('{"model":"withdrawn"}')
    assert auto.initial_model(catalog, None, resume=False) == ("claude-sonnet-4-6", True)


@pytest.mark.parametrize("output", ["Kept model as Auto", "Model switch cancelled"])
def test_picker_cancel_does_not_open_chooser(selection, output):
    selection.observe("", output)
    assert not selection.choose
    assert selection.enabled


def test_auto_confirmation_opens_chooser_without_saving(selection):
    selection.observe("", "Set model to Auto and saved as your default for new sessions")
    assert selection.choose and selection.alias_selected
    assert not selection.enabled
    assert not auto.preference_path(selection.auto).exists()


def test_session_only_auto_with_effort_opens_chooser(selection):
    selection.observe("", "Set model to ug-auto-sonnet for this session only with high effort")
    assert selection.choose
    assert selection.alias_selected


def test_internal_switch_restores_real_target_of_native_saved_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "APP_DIR", tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text('{"model":"ug-auto-sonnet","unrelated":true}')
    guard = v2._ClaudeModelSettingGuard(
        settings, alias_targets={"ug-auto-sonnet": "claude-sonnet-4-6"}
    )
    guard.begin("claude-haiku-4-5")
    settings.write_text('{"model":"claude-haiku-4-5","unrelated":true}')
    assert guard.is_routed()
    guard.restore()
    assert read_json_safe(settings) == {"model": "claude-sonnet-4-6", "unrelated": True}


def test_regular_model_turns_routing_off(selection):
    selection.observe(
        "",
        "Set model to Haiku 4.5 and saved as your default for new sessions",
        saved_model="claude-haiku-4-5",
    )
    assert not selection.enabled
    assert set(read_json_safe(selection.controls).values()) == {"0"}
    assert selection.options()[0][0] == "claude-haiku-4-5"


@pytest.mark.parametrize("accepted", [False, True])
def test_only_confirmed_actual_model_is_saved(selection, accepted):
    selection.pending = auto.ModelSwitch("claude-haiku-4-5", True)
    selection.set_enabled(False)
    notice = selection.observe("claude-haiku-4-5", "Set model to Haiku 4.5", accepted=accepted)
    assert selection.enabled is accepted
    assert auto.preference_path(selection.auto).exists() is accepted
    if accepted:
        assert read_json_safe(auto.preference_path(selection.auto)) == {"model": "claude-haiku-4-5"}
        assert "subagents smart routed" in notice


def command_rows(args="", output="Set model to Auto", **overrides):
    base = {"type": "user", "sessionId": "session", "isSidechain": False, **overrides}
    return [
        {
            **base,
            "uuid": "command",
            "message": {
                "content": f"<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args>{args}</command-args>"
            },
        },
        {
            **base,
            "parentUuid": "command",
            "message": {"content": f"<local-command-stdout>{output}</local-command-stdout>"},
        },
    ]


def test_only_complete_new_native_command_pairs_are_observed(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(map(json.dumps, command_rows())) + "\n")
    session = tmp_path / "session.json"
    auto.session_hook(session, {"transcript_path": str(transcript), "session_id": "session"})
    reader = auto.ModelCommands(session)
    assert reader.poll() == []  # Resume never replays the last model command.
    rows = command_rows(output="Set model to \x1b[1mAuto\x1b[22m")
    with transcript.open("a") as stream:
        stream.write(json.dumps(rows[0]) + "\n" + json.dumps(rows[1]))
    assert reader.poll() == []
    with transcript.open("a") as stream:
        stream.write("\n")
    assert reader.poll() == [("", "Set model to Auto")]
    assert reader.poll() == []


@pytest.mark.parametrize(
    "overrides", [{"type": "assistant"}, {"isSidechain": True}, {"sessionId": "other"}]
)
def test_assistant_and_other_session_content_cannot_open_chooser(tmp_path, overrides):
    transcript = tmp_path / "transcript.jsonl"
    session = tmp_path / "session.json"
    auto.session_hook(session, {"transcript_path": str(transcript), "session_id": "session"})
    transcript.write_text("\n".join(map(json.dumps, command_rows(**overrides))) + "\n")
    assert auto.ModelCommands(session).poll() == []


@pytest.mark.parametrize(
    "data",
    [
        b"discuss /model auto",
        b"\x1b[200~explain this:\n/model auto\x1b[201~",
        b"/model auto\x1b[D",
        b"/model auto\nother",
    ],
)
def test_nonliteral_input_is_forwarded(data):
    parser = auto.AutoCommand()
    assert not parser.feed(data)
    assert not parser.feed(b"\r")


def test_exact_command_is_intercepted_at_submit():
    parser = auto.AutoCommand()
    for data in (b"/model", b" aut", b"o"):
        assert not parser.feed(data)
    assert parser.feed(b"\r")


def test_terminal_replies_do_not_lose_command_input():
    parser = auto.AutoCommand()
    parser.feed(b"\x1b[1;1R")
    parser.feed(b"/model auto")
    assert parser.feed(b"\r")


def test_single_line_paste_can_be_submitted_as_a_command():
    parser = auto.AutoCommand()
    assert not parser.feed(b"\x1b[200~/model auto\x1b[201~")
    assert parser.feed(b"\r")


def test_native_cache_warning_cancel_preserves_auto(selection):
    selection.pending = auto.ModelSwitch("claude-haiku-4-5", True)
    selection.set_enabled(False)
    selection.observe("claude-haiku-4-5", "Kept model as Sonnet 4.6")
    assert selection.enabled
    assert selection.model == "claude-sonnet-4-6"
    assert not auto.preference_path(selection.auto).exists()


def test_explicit_auto_alias_arguments_resolve_without_changing_prompt(catalog):
    args = ["--model=ug-auto-sonnet", "--", "--model", "ug-auto-haiku"]
    assert auto.resolve_model_args(args, catalog) == [
        "--model=claude-sonnet-4-6",
        "--",
        "--model",
        "ug-auto-haiku",
    ]
    assert auto.resolve_model_args(["--model", "ug-auto-haiku"], catalog) == [
        "--model",
        "claude-haiku-4-5",
    ]


def test_launch_model_precedence_over_saved_preference(catalog):
    path = auto.preference_path(catalog)
    path.parent.mkdir()
    path.write_text('{"model":"claude-haiku-4-5"}')
    assert auto.initial_model({**catalog, "preferSaved": False}, None, resume=False) == (
        "claude-sonnet-4-6",
        True,
    )


def test_pre_mod_plugin_has_roles_without_unsupported_hooks(tmp_path):
    v2._write_routed_claude_plugin(tmp_path, ["claude-sonnet-4-6"], native_modules=False)
    assert list((tmp_path / "agents").glob("*.md"))
    assert not (tmp_path / "hooks").exists()


def test_legacy_routing_hook_preserves_foreign_handlers():
    foreign = {"hooks": [{"type": "command", "command": "user-hook"}]}
    settings = {"hooks": {"PreToolUse": [foreign]}}
    claude_hooks.sync_smart_routing_hooks(
        settings,
        {
            "workspace": "https://workspace.example",
            "claude_models": {"sonnet": "claude-sonnet-4-6"},
        },
        enabled=True,
        legacy=True,
    )
    handlers = settings["hooks"]["PreToolUse"]
    assert handlers[0] == foreign
    assert handlers[1]["matcher"] == "Agent|Task"
    assert "route-subagent --host https://workspace.example" in handlers[1]["hooks"][0]["command"]


@pytest.mark.skipif(os.name == "nt", reason="The pre-mod terminal chooser uses POSIX PTYs")
@pytest.mark.parametrize(
    "args,enabled", [([], True), (["--model", "claude-haiku-4-5"], False), (["--resume"], False)]
)
def test_236_launch_uses_terminal_chooser_and_command_hooks(tmp_path, monkeypatch, args, enabled):
    monkeypatch.setenv("SMART_ROUTER_CONFIG_VERSION", "subagent_auto_v0_claude_only")
    monkeypatch.delenv("ISAAC_SMART_ROUTER_FEEDBACK", raising=False)
    monkeypatch.setattr(claude, "agent_version", lambda _: "2.1.236")
    managed = tmp_path / "managed.json"
    document = add_picker_entry(
        {
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [
                    {"model": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
                    {"model": "claude-haiku-4-5", "label": "Haiku 4.5"},
                ],
            }
        },
        static_auto_row=True,
    )
    managed.write_text(json.dumps(document))
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed)
    monkeypatch.setattr(v2, "APP_DIR", tmp_path)
    monkeypatch.setattr(auto, "APP_DIR", tmp_path)
    monkeypatch.setattr(custom_oauth, "get_databricks_token", lambda *_a, **_kw: "fixture-token")
    monkeypatch.setattr(v2, "build_auth_token_argv", lambda *_a, **_kw: ["/bin/ug"])
    monkeypatch.setattr("ucode.databricks.ug_binary", lambda: "/bin/ug")
    controls = tmp_path / "controls.json"
    controls.write_text("{}")
    monkeypatch.setenv(v2.SESSION_ENV_VAR, str(controls))
    monkeypatch.setenv(v2.SESSION_PYTHON_ENV_VAR, sys.executable)
    monkeypatch.setattr(v2, "_prepare_smart_router_session", lambda _: controls)
    captured = {}

    def launch(argv, **kwargs):
        captured.update(kwargs)
        settings = read_json_safe(Path(argv[argv.index("--settings") + 1]))
        plugin = Path(argv[argv.index("--plugin-dir") + 1])
        assert not (plugin / "hooks/hooks.json").exists()
        assert list((plugin / "agents").glob("*.md"))
        assert "route-subagent --host" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert (
            "claude_legacy_auto session"
            in settings["hooks"]["SessionStart"][-1]["hooks"][0]["command"]
        )
        assert "--system-prompt-snapshot" not in argv
        return 7

    monkeypatch.setattr(auto, "run", launch)
    with pytest.raises(SystemExit, match="7"):
        v2.launch_claude(
            {"workspace": "https://workspace.example"},
            args,
            binary="claude",
            user_settings_path=tmp_path / "user.json",
            launch_model=None,
            compose_settings=lambda args: ({}, args),
            launch_model_args=claude._launch_model_args,
            model_name=claude._maybe_add_1m_suffix,
        )
    assert captured["enabled"] is enabled
    assert read_json_safe(managed) == document
