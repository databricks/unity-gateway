"""Component guards for startup imports and compatible ug/ucode version reporting."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest

from ucode import entrypoint

_LAZY_MODULES = (
    "databricks.sdk",
    "httpx",
    "mcp",
    "websockets",
    "ucode.gateway_proxy",
    "ucode.smart_routing.codex_interposer",
)


def _modules_loaded_after(code: str, modules: tuple[str, ...]) -> tuple[str, list[str]]:
    """Run code in a fresh interpreter and report the guarded imports."""
    probe = f"{code}\nimport json, sys\nprint(json.dumps([m for m in {list(modules)!r} if m in sys.modules]))"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    *output, loaded = result.stdout.splitlines()
    return "\n".join(output), json.loads(loaded)


def _write_metadata(root: Path, reported_version: str, distribution: str = "unity_gateway") -> Path:
    metadata = root / f"{distribution}-{reported_version}.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        f"Metadata-Version: 2.4\nName: unity-gateway\nVersion: {reported_version}\n",
        encoding="utf-8",
    )
    return metadata


@pytest.mark.parametrize("command", ["ug", "ucode"])
@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_entrypoint_skips_cli_imports(command: str, flag: str) -> None:
    output, loaded = _modules_loaded_after(
        f"import sys\nsys.argv = [{command!r}, {flag!r}]\nfrom ucode.entrypoint import main\nmain()",
        ("ucode.cli", "typer", "ucode.telemetry", "importlib.metadata", *_LAZY_MODULES),
    )

    assert output == version("unity-gateway")
    assert loaded == []


@pytest.mark.parametrize("command", ["ug", "ucode"])
@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_output_does_not_establish_cli_health(
    tmp_path: Path, command: str, flag: str
) -> None:
    _write_metadata(tmp_path, "1.2.3")
    source = Path(entrypoint.__file__).resolve().parents[1]
    probe = (
        f"import sys\nsys.path[:0] = {[str(tmp_path), str(source)]!r}\n"
        f"sys.argv = [{command!r}, sys.argv[1]]\n"
        "from ucode.entrypoint import main\nmain()"
    )
    # Disable site packages so metadata survives while startup dependencies are absent.
    command_line = [sys.executable, "-I", "-S", "-c", probe]
    reported = subprocess.run(
        [*command_line, flag],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    health = subprocess.run(
        [*command_line, "--help"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert reported.returncode == 0, reported.stderr
    assert reported.stdout == "1.2.3\n"
    assert reported.stderr == ""
    assert health.returncode != 0
    assert "ModuleNotFoundError: No module named 'typer'" in health.stderr


def test_installed_version_matches_importlib_metadata() -> None:
    assert entrypoint._read_installed_version() == version("unity-gateway")


def test_cli_import_defers_subcommand_specific_modules() -> None:
    _, loaded = _modules_loaded_after("import ucode.cli\nimport ucode.agents.claude", _LAZY_MODULES)

    assert loaded == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("auth-token", "helper-bearer"),
        ("otel-headers", '{"Authorization": "Bearer helper-bearer"}'),
    ],
)
@pytest.mark.parametrize("saved", [False, True], ids=["explicit-host", "saved-pat"])
def test_agent_helper_entrypoint_skips_the_cli(tmp_path, command, expected, saved) -> None:
    workspace = "https://ws.example"
    environment = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PATH": ""}
    arguments = ["--host", workspace]
    if saved:
        app_dir = tmp_path / ".ucode"
        app_dir.mkdir(exist_ok=True)
        app_dir.joinpath("state.json").write_text(
            json.dumps(
                {
                    "state_version": 3,
                    "config_scope": "user",
                    "current_workspace": workspace,
                    "workspaces": {workspace: {"profile": "SAVED", "use_pat": True}},
                }
            )
        )
        profile = tmp_path / ".databrickscfg"
        profile.write_text(f"[SAVED]\nhost = {workspace}\ntoken = helper-bearer\n")
        environment.update(DATABRICKS_CONFIG_FILE=str(profile), DATABRICKS_BEARER="")
        arguments = []
    else:
        environment["DATABRICKS_BEARER"] = "helper-bearer"
    probe = (
        "import os, sys\n"
        f"os.environ.update({environment!r})\n"
        f"sys.argv = {['ug', command, *arguments]!r}\n"
    )
    if saved:
        # Only external profile discovery is stubbed; the temporary PAT file is read normally.
        probe += (
            "import ucode.databricks as db\n"
            "db.list_profile_entries = lambda: [{'name': 'SAVED', 'auth_type': 'pat'}]\n"
        )
    probe += (
        "from ucode.entrypoint import main\n"
        "try:\n    main()\nexcept SystemExit as exc:\n    assert not exc.code, exc.code\n"
    )
    if saved:
        probe += "from ucode.managed_files import config_scope\nassert config_scope() == 'user'\n"

    output, loaded = _modules_loaded_after(probe, ("ucode.cli", "ucode.agents", *_LAZY_MODULES))

    assert output == expected
    assert loaded == []


@pytest.mark.parametrize(
    ("command", "event", "payload", "artifact"),
    [
        (
            "claude-router-hook",
            "session-start",
            {"session_id": "s1"},
            "claude-smart-routing-canary.json",
        ),
        (
            "claude-router-hook",
            "record-subagent",
            {"session_id": "s1"},
            "claude-smart-routing-audit.jsonl",
        ),
        (
            "codex-router-hook",
            "session-start",
            {"session_id": "s1"},
            "codex-smart-routing-canary.json",
        ),
        (
            "codex-router-hook",
            "record-subagent",
            {"session_id": "s1"},
            "codex-smart-routing-audit.jsonl",
        ),
    ],
)
def test_router_hook_records_skip_the_cli(tmp_path, command, event, payload, artifact) -> None:
    # Agents run these at session start and before every subagent starts.
    session = tmp_path / "env.json"
    session.write_text("{}", encoding="utf-8")
    probe = (
        "import io, os, sys\n"
        f"os.environ.update(HOME={str(tmp_path)!r}, USERPROFILE={str(tmp_path)!r}, "
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY='1', "
        f"UCODE_SESSION_ENV_FILE={str(session)!r})\n"
        f"sys.argv, sys.stdin = ['ug', {command!r}, {event!r}], io.TextIOWrapper(io.BytesIO({json.dumps(payload)!r}.encode()), encoding='cp1252')\n"
        "from ucode.entrypoint import main\n"
        "try:\n    main()\nexcept SystemExit as exc:\n    assert not exc.code, exc.code"
    )
    deferred = ("ucode.cli", "ucode.databricks", "ucode.smart_routing.v2", "ucode.ui")

    output, loaded = _modules_loaded_after(probe, (*deferred, *_LAZY_MODULES))

    assert output == ""
    assert loaded == []
    assert '"session_id": "s1"' in (tmp_path / ".ucode" / artifact).read_text(encoding="utf-8")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_router_helper_entrypoint_applies_saved_user_scope(tmp_path, monkeypatch, agent):
    from io import BytesIO, TextIOWrapper

    from ucode import managed_files, state
    from ucode.smart_routing import codex_routing, hook_commands, v2

    state.save_config_scope("user")
    managed_files.set_config_scope("global")
    session = tmp_path / "session.json"
    session.write_text("{}")
    monkeypatch.setenv("UCODE_SESSION_ENV_FILE", str(session))
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.setattr(hook_commands, "_router_hook_token", lambda *_args: "token")
    observed = []

    def route(*args, **kwargs):
        observed.append(managed_files.config_scope())

    if agent == "claude":
        monkeypatch.setattr(v2, "route_claude_subagent", route)
    else:
        monkeypatch.setattr(codex_routing, "route_pre_tool_use", route)
    monkeypatch.setattr(sys, "stdin", TextIOWrapper(BytesIO(b"{}")))
    monkeypatch.setattr(
        sys,
        "argv",
        ["ug", f"{agent}-router-hook", "route-subagent", "--host", "https://ws.example"],
    )

    with pytest.raises(SystemExit) as exc:
        entrypoint.main()

    assert exc.value.code == 0
    assert observed == ["user"]


def test_agent_helpers_are_the_cli_hidden_helper_commands() -> None:
    from importlib import import_module

    from typer.main import get_command

    from ucode import cli

    commands = get_command(cli.app).commands
    for module in set(entrypoint._AGENT_HELPERS.values()):
        names = {name for name, owner in entrypoint._AGENT_HELPERS.items() if owner == module}
        assert names == set(import_module(module).COMMANDS)
    assert all(commands[name].hidden for name in entrypoint._AGENT_HELPERS)


@pytest.mark.parametrize(
    ("command", "arguments", "expected_exit"),
    [
        (command, arguments, expected_exit)
        for command in ["auth-token", "otel-headers", "claude-router-hook", "codex-router-hook"]
        for arguments, expected_exit in [(["--help"], 0), (["--unknown-option"], 2)]
    ]
    + [(command, [], 2) for command in ["claude-router-hook", "codex-router-hook"]],
)
@pytest.mark.parametrize("executable", ["ug", "ucode"])
def test_agent_helper_usage_matches_the_cli(command, arguments, expected_exit, executable) -> None:
    def command_output(module: str) -> tuple[str, str]:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys\nsys.argv = {[executable, command, *arguments]!r}\n"
                f"from {module} import main\nmain()",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={**os.environ, "COLUMNS": "100", "NO_COLOR": "1"},
        )
        assert result.returncode == expected_exit, result.stderr
        return result.stdout, result.stderr

    assert command_output("ucode.entrypoint") == command_output("ucode.cli")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_orchestrator_prompt_hook_skips_cli_imports(tmp_path, monkeypatch, agent) -> None:
    # Agents run this hook before every prompt is submitted.
    # A conflicting inherited selector verifies that the child isolates its explicit flags.
    monkeypatch.setenv("SMART_ROUTER_CONFIG_VERSION", "subagent_only_v0")
    session = tmp_path / "env.json"
    session.write_text("{}", encoding="utf-8")
    probe = (
        "import os\n"
        "from ucode.constants import SMART_ROUTING_ENV_KEYS, SMART_ROUTER_CONFIG_VERSION_ENV_VAR\n"
        "for key in (*SMART_ROUTING_ENV_KEYS, SMART_ROUTER_CONFIG_VERSION_ENV_VAR):\n"
        "    os.environ.pop(key, None)\n"
        "os.environ.update(ENABLE_SMART_ROUTER_ORCHESTRATOR='1', "
        f"ENABLE_SMART_ROUTING_SUBAGENT_ONLY='1', UCODE_SESSION_ENV_FILE={str(session)!r})\n"
        "from ucode.smart_routing import orchestrator\n"
        "hook = orchestrator.hook_output({'hook_event_name': 'UserPromptSubmit'}, "
        f"agent={agent!r})\n"
        "print('is on for this session' in hook['hookSpecificOutput']['additionalContext'])"
    )
    deferred = (
        "ucode.cli",
        "typer",
        "importlib.metadata",
        "rich",
        "tomlkit",
        "ucode.databricks",
        "ucode.smart_routing.v2",
        "ucode.ui",
        *_LAZY_MODULES,
    )

    output, loaded = _modules_loaded_after(probe, deferred)

    assert output == "True"
    assert loaded == []


@pytest.mark.parametrize("reported_version", ["1.2.3", "1.2.3.post4", "1.2.3+abc123"])
def test_metadata_reader_preserves_version_suffixes(
    tmp_path, monkeypatch, reported_version
) -> None:
    _write_metadata(tmp_path, reported_version)
    monkeypatch.setattr(sys, "path", [str(tmp_path)])

    assert entrypoint._read_installed_version() == version("unity-gateway") == reported_version


@pytest.mark.parametrize("distribution", ["unity_gateway", "UNITY_GATEWAY", "unity.gateway"])
def test_metadata_reader_normalizes_distribution_name(tmp_path, monkeypatch, distribution) -> None:
    _write_metadata(tmp_path, "1.2.3", distribution)
    _write_metadata(tmp_path, "9.9.9", "unity_gateway_extra")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])

    assert entrypoint._read_installed_version() == version("unity-gateway") == "1.2.3"


def test_metadata_reader_uses_first_sys_path_match(tmp_path, monkeypatch) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_metadata(first, "1.2.3")
    _write_metadata(second, "4.5.6")
    monkeypatch.setattr(sys, "path", [str(first), str(second)])

    assert entrypoint._read_installed_version() == version("unity-gateway") == "1.2.3"


def test_metadata_reader_handles_empty_sys_path_entry(tmp_path, monkeypatch) -> None:
    _write_metadata(tmp_path, "1.2.3")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [""])

    assert entrypoint._read_installed_version() == version("unity-gateway") == "1.2.3"


def test_metadata_reader_skips_missing_sys_path_directory(tmp_path, monkeypatch) -> None:
    _write_metadata(tmp_path, "1.2.3")
    monkeypatch.setattr(sys, "path", [str(tmp_path / "missing"), str(tmp_path)])

    assert entrypoint._read_installed_version() == "1.2.3"


@pytest.mark.parametrize(
    "contents",
    [None, "Name: unity-gateway\nVersion: \n", "Name: unity-gateway\n\nVersion: 1.2.3\n"],
)
def test_metadata_reader_falls_back_for_incomplete_metadata(
    tmp_path, monkeypatch, contents
) -> None:
    metadata = _write_metadata(tmp_path, "1.2.3")
    if contents is None:
        metadata.unlink()
    else:
        metadata.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])

    assert entrypoint._read_installed_version() is None


def test_metadata_reader_falls_back_for_zip_path_before_directory(tmp_path, monkeypatch) -> None:
    archive = tmp_path / "installed.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(
            "unity_gateway-1.2.3.dist-info/METADATA", "Name: unity-gateway\nVersion: 1.2.3\n"
        )
    directory = tmp_path / "later"
    _write_metadata(directory, "4.5.6")
    monkeypatch.setattr(sys, "path", [str(archive), str(directory)])

    assert entrypoint._read_installed_version() is None
    assert version("unity-gateway") == "1.2.3"


def test_metadata_reader_skips_windows_console_script_launcher(tmp_path, monkeypatch) -> None:
    # Windows console scripts put their launcher (e.g. Scripts\ug.exe) first on sys.path.
    launcher = tmp_path / "ug.exe"
    launcher.write_bytes(b"MZ")
    directory = tmp_path / "site-packages"
    _write_metadata(directory, "1.2.3")
    monkeypatch.setattr(sys, "path", [str(launcher), str(directory)])

    assert entrypoint._read_installed_version() == version("unity-gateway") == "1.2.3"


def test_metadata_reader_falls_back_for_egg_info(tmp_path, monkeypatch) -> None:
    egg = tmp_path / "unity_gateway.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text("Name: unity-gateway\nVersion: 1.2.3\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])

    assert entrypoint._read_installed_version() is None
    assert version("unity-gateway") == "1.2.3"


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_entrypoint_falls_back_to_telemetry(monkeypatch, capsys, flag) -> None:
    from ucode import telemetry

    monkeypatch.setattr(sys, "argv", ["ug", flag])
    monkeypatch.setattr(entrypoint, "_read_installed_version", lambda: None)
    monkeypatch.setattr(telemetry, "ug_version", lambda: "1.2.3.post4+abc123")

    entrypoint.main()

    assert capsys.readouterr().out == "1.2.3.post4+abc123\n"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--help"],
        ["--dry-run", "--version"],
        ["--header", "X-Test: yes", "auth-token"],
        ["--version", "foo"],
        ["--version", "--help"],
        ["-V", "claude"],
        ["claude", "--version"],
    ],
)
def test_other_arguments_delegate_to_cli_unchanged(monkeypatch, argv) -> None:
    from ucode import cli

    calls = []
    monkeypatch.setattr(sys, "argv", ["ug", *argv])
    monkeypatch.setattr(cli, "main", lambda: calls.append(sys.argv.copy()))
    monkeypatch.setattr(
        entrypoint, "_read_installed_version", lambda: pytest.fail("unexpected version fast path")
    )

    entrypoint.main()

    assert calls == [["ug", *argv]]
