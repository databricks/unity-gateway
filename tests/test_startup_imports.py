"""Component guards for startup imports and compatible ug/ucode version reporting."""

from __future__ import annotations

import json
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
    "questionary",
    "prompt_toolkit",
    "ucode.ui.interactive_picker",
    "rich.progress",
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
    ("arguments", "routing_enabled"),
    [
        (["--help"], False),
        (["claude", "--help"], False),
        (["claude-router-hook", "route-first-prompt"], False),
        (["codex-router-hook", "session-start"], False),
        (["claude-router-hook", "session-start"], True),
        (["codex-router-hook", "session-start"], True),
    ],
)
def test_noninteractive_commands_skip_picker_imports(
    tmp_path, monkeypatch, arguments, routing_enabled
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("ENABLE_SMART_ROUTING_V2", "1" if routing_enabled else "0")
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "0")
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.delenv("UCODE_SESSION_ENV_FILE", raising=False)
    output, loaded = _modules_loaded_after(
        "import sys\nfrom io import BytesIO, TextIOWrapper\n"
        'sys.stdin = TextIOWrapper(BytesIO(b\'{"session_id": "startup-import-test"}\'))\n'
        f"sys.argv = {['ug', *arguments]!r}\n"
        "from ucode.entrypoint import main\n"
        "try:\n"
        "    main()\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0, exc.code",
        _LAZY_MODULES,
    )

    assert loaded == []
    if "--help" in arguments:
        assert "Usage:" in output
    else:
        assert output == ""
        if routing_enabled:
            agent = arguments[0].removesuffix("-router-hook")
            canary = tmp_path / ".ucode" / f"{agent}-smart-routing-canary.json"
            assert json.loads(canary.read_text())["session_id"] == "startup-import-test"


def test_public_picker_exports_preserve_identity_and_parameters():
    from inspect import signature

    from ucode import ui
    from ucode.ui import interactive_picker

    assert ui._BACK is interactive_picker._BACK
    assert ui._Back is interactive_picker._Back
    assert ui.StreamingInquirerControl is interactive_picker.StreamingInquirerControl
    # Keep the lazy wrapper typed without letting its parameters or defaults drift.
    assert (
        signature(ui.scrolling_checkbox).parameters
        == signature(interactive_picker.scrolling_checkbox).parameters
    )

    namespace = {}
    exec("from ucode.ui import *", namespace)
    assert namespace["StreamingInquirerControl"] is interactive_picker.StreamingInquirerControl
    assert namespace["merge_new_choices"] is interactive_picker.merge_new_choices
    for name in ("Window", "Dimension", "InquirerControl"):
        assert namespace[name] is getattr(interactive_picker, name)
    from prompt_toolkit.formatted_text import to_formatted_text
    from questionary.constants import INDICATOR_SELECTED, INDICATOR_UNSELECTED

    assert namespace["to_formatted_text"] is to_formatted_text
    assert namespace["INDICATOR_SELECTED"] == INDICATOR_SELECTED
    assert namespace["INDICATOR_UNSELECTED"] == INDICATOR_UNSELECTED


def test_public_progress_exports_preserve_identity():
    from rich import progress

    from ucode import ui
    from ucode.ui import BarColumn, MofNCompleteColumn, Progress, TextColumn

    namespace = {}
    exec("from ucode.ui import *", namespace)
    for exported in (BarColumn, MofNCompleteColumn, Progress, TextColumn):
        assert exported is getattr(progress, exported.__name__)
        assert exported is getattr(ui, exported.__name__)
        assert exported is namespace[exported.__name__]


def test_redirected_progress_bar_keeps_rich_progress_unloaded() -> None:
    output, loaded = _modules_loaded_after(
        "from ucode.ui import progress_bar\n"
        "with progress_bar('Loading', 2) as advance:\n"
        "    advance()\n"
        "    advance()",
        ("rich.progress",),
    )

    assert output == ""
    assert loaded == []


def test_auth_token_helper_skips_picker_imports(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("DATABRICKS_BEARER", "ug-startup-test-bearer")
    output, loaded = _modules_loaded_after(
        "import sys\n"
        "from contextlib import redirect_stdout\n"
        "from io import StringIO\n"
        "sys.argv = ['ug', 'auth-token', '--host', 'https://workspace.example.invalid']\n"
        "from ucode.entrypoint import main\n"
        "with redirect_stdout(StringIO()) as output:\n"
        "    try:\n"
        "        main()\n"
        "    except SystemExit as exc:\n"
        "        assert exc.code == 0, exc.code\n"
        "assert output.getvalue() == 'ug-startup-test-bearer\\n', output.getvalue()",
        _LAZY_MODULES,
    )

    assert output == ""
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
