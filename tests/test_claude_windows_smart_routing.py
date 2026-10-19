"""Portable regression coverage for Claude's Windows smart-routing launch."""

from __future__ import annotations

import builtins
import json
from pathlib import Path, PosixPath

from ucode import config_io
from ucode.agents import LaunchOptions, SmartRouterLaunch, claude
from ucode.databricks import AnthropicModelCatalog
from ucode.smart_routing import session_env, v2


def _plugin_agent_models(plugin_dir: Path) -> set[str]:
    models = set()
    for agent_path in (plugin_dir / "agents").glob("*.md"):
        model_line = next(
            line for line in agent_path.read_text().splitlines() if line.startswith("model: ")
        )
        models.add(json.loads(model_line.removeprefix("model: ")))
    return models


def test_windows_first_prompt_falls_back_to_subagent_routing_without_unix_imports(
    tmp_path, monkeypatch
):
    """Windows routes subagents on the normal launch, without importing the Unix PTY or fcntl."""
    shim = tmp_path / "npm prefix" / "claude.cmd"
    native_binary = (
        shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    )
    native_binary.parent.mkdir(parents=True)
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
    warnings: list[str] = []
    launches: list[list[str]] = []

    host_os_name = v2.os.name
    path_type = type(tmp_path)
    monkeypatch.setattr(v2.os, "name", "nt")
    # ``pathlib.Path`` follows the process-wide os.name even on this Linux test host. Keep the
    # filesystem seam POSIX while exercising the Windows routing branch.
    if host_os_name != "nt":
        monkeypatch.setattr(v2, "Path", PosixPath)
        monkeypatch.setattr(session_env, "Path", PosixPath)
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
    monkeypatch.delenv(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR, raising=False)
    monkeypatch.setattr(v2, "install_skill", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(v2, "_model_picker_catalog", lambda: None)
    monkeypatch.setattr(v2, "get_databricks_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(
        v2,
        "list_anthropic_model_catalog",
        lambda *_args: AnthropicModelCatalog(
            model_ids=["system.ai.claude-opus-4-8"], model_id_to_display_name={}
        ),
    )
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", tmp_path / "ucode-settings.json")
    monkeypatch.setattr(claude.shutil, "which", lambda _binary: str(shim))
    monkeypatch.setattr(claude, "get_databricks_token", lambda *_args: "token")
    monkeypatch.setattr(claude, "print_warning", warnings.append)
    monkeypatch.setattr(claude, "exec_or_spawn", launches.append)

    real_import = builtins.__import__

    def forbid_unix_routing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"fcntl", "ucode.smart_routing.claude_pty"}:
            raise AssertionError(f"Windows subagent routing imported Unix-only module {name}")
        if name == "ucode.smart_routing" and "claude_pty" in fromlist:
            raise AssertionError("Windows subagent routing imported the Unix-only Claude PTY")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", forbid_unix_routing_import)

    claude.launch(
        {"workspace": "https://example.com"},
        caller_args,
        options=LaunchOptions(smart_router=SmartRouterLaunch(first_prompt=True)),
    )

    assert warnings == [
        "Claude first-prompt smart routing is unavailable on Windows; using subagent-only routing."
    ]
    (argv,) = launches
    settings = json.loads(argv[argv.index("--settings") + 1])
    plugin_dir = path_type(argv[argv.index("--plugin-dir") + 1])
    assert argv[0] == str(native_binary)
    assert argv.count("--plugin-dir") == 2
    assert argv[-len(caller_args) :] == caller_args
    assert plugin_dir.parent == config_io.APP_DIR / v2.CLAUDE_ROUTING_PLUGIN_DIRNAME
    assert _plugin_agent_models(plugin_dir) == {"system.ai.claude-opus-4-8"}
    assert settings["env"][v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR] == "1"
    assert v2.ENABLE_SMART_ROUTING_ENV_VAR not in settings["env"]
    assert "UserPromptSubmit" not in settings["hooks"]
    assert "PreToolUse" not in settings["hooks"]
    command = settings["pluginConfigs"][v2.CLAUDE_ROUTING_PLUGIN_NAME]["options"]["routeCommand"]
    assert command[1:3] == ["claude-router-hook", "route-subagent-mod"]
    assert settings["modelOverrides"] == {"claude-opus-4-8": "system.ai.claude-opus-4-8"}
