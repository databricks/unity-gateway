"""Portable regression coverage for Claude's Windows smart-routing launch."""

from __future__ import annotations

import builtins
import json
from pathlib import Path, PosixPath

import pytest

from ucode.agents import claude
from ucode.databricks import AnthropicModelCatalog
from ucode.smart_routing import orchestrator, session_env, v2


def _plugin_agent_models(plugin_dir: Path) -> set[str]:
    models = set()
    for agent_path in (plugin_dir / "agents").glob(f"{v2.CLAUDE_ROUTED_AGENT_PREFIX}*.md"):
        model_line = next(
            line for line in agent_path.read_text().splitlines() if line.startswith("model: ")
        )
        models.add(json.loads(model_line.removeprefix("model: ")))
    return models


def test_windows_subagent_routing_uses_native_binary_without_unix_imports(tmp_path, monkeypatch):
    """Windows uses hooks/plugin routing directly, without importing the Unix PTY or fcntl."""
    user_settings = tmp_path / "settings.json"
    user_settings.write_text(json.dumps({"model": "opus"}))
    native_binary = tmp_path / "Claude Code" / "claude.exe"
    native_binary.parent.mkdir()
    native_binary.touch()
    caller_plugin = tmp_path / "caller plugin"
    caller_args = [
        "--agents",
        json.dumps({"reviewer": {"description": "Review code", "prompt": "Review it."}}),
        "--plugin-dir",
        str(caller_plugin),
        "--",
        "prompt",
    ]
    captured: dict = {}
    warnings: list[str] = []

    host_os_name = v2.os.name
    skill_directory = orchestrator.skill_directory()
    monkeypatch.setattr(orchestrator, "skill_directory", lambda: skill_directory)
    path_type = type(tmp_path)
    monkeypatch.setattr(v2.os, "name", "nt")
    # ``pathlib.Path`` follows the process-wide os.name even on this Linux test host. Keep the
    # filesystem seam POSIX while exercising the Windows routing branch.
    if host_os_name != "nt":
        monkeypatch.setattr(v2, "Path", PosixPath)
        monkeypatch.setattr(session_env, "Path", PosixPath)
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
    monkeypatch.delenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, raising=False)
    monkeypatch.setattr(v2, "APP_DIR", tmp_path)
    monkeypatch.setattr(v2, "install_skill", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(v2, "_model_picker_catalog", lambda: None)
    monkeypatch.setattr(v2, "get_databricks_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(v2, "build_auth_token_argv", lambda *_args, **_kwargs: ["ug"])
    monkeypatch.setattr(
        v2,
        "list_anthropic_model_catalog",
        lambda *_args: AnthropicModelCatalog(
            model_ids=["system.ai.claude-opus-4-8"], model_id_to_display_name={}
        ),
    )
    monkeypatch.setattr(v2, "print_warning", warnings.append)

    real_import = builtins.__import__

    def forbid_unix_routing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"fcntl", "ucode.smart_routing.claude_pty"}:
            raise AssertionError(f"Windows subagent routing imported Unix-only module {name}")
        if name == "ucode.smart_routing" and "claude_pty" in fromlist:
            raise AssertionError("Windows subagent routing imported the Unix-only Claude PTY")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", forbid_unix_routing_import)

    class FakeProcess:
        def __init__(self, argv, **_kwargs):
            captured["argv"] = argv
            settings_path = path_type(argv[argv.index("--settings") + 1])
            plugin_dir = path_type(argv[argv.index("--plugin-dir") + 1])
            captured["settings_path"] = settings_path
            captured["plugin_dir"] = plugin_dir
            captured["settings"] = json.loads(settings_path.read_text())
            captured["plugin_models"] = _plugin_agent_models(plugin_dir)

        def wait(self):
            return 7

        def send_signal(self, _signal):
            raise AssertionError("test does not interrupt Claude")

    monkeypatch.setattr(v2.subprocess, "Popen", FakeProcess)

    with pytest.raises(SystemExit) as exc:
        v2.launch_claude(
            {"workspace": "https://example.com"},
            caller_args,
            binary=str(native_binary),
            user_settings_path=user_settings,
            launch_model="opus",
            compose_settings=lambda args: ({}, args),
            launch_model_args=claude._launch_model_args,
            model_name=claude._maybe_add_1m_suffix,
        )

    assert exc.value.code == 7
    assert warnings == [
        "Claude first-prompt smart routing is unavailable on Windows; using subagent-only routing."
    ]
    settings = captured["settings"]
    assert captured["argv"][0] == str(native_binary)
    assert captured["argv"].count("--plugin-dir") == 2
    assert captured["argv"][-len(caller_args) :] == caller_args
    assert captured["plugin_models"] == {"system.ai.claude-opus-4-8"}
    assert settings["env"][v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "1"
    assert v2.ENABLE_SMART_ROUTING_ENV_VAR not in settings["env"]
    assert "route-first-prompt" not in str(settings["hooks"])
    assert "ucode.smart_routing.orchestrator" in str(settings["hooks"]["UserPromptSubmit"])
    assert "route-subagent" in str(settings["hooks"]["PreToolUse"])
    assert settings["modelOverrides"] == {"claude-opus-4-8": "system.ai.claude-opus-4-8"}
    assert not captured["settings_path"].exists()
    assert not captured["plugin_dir"].exists()
    assert json.loads(user_settings.read_text()) == {"model": "opus"}
