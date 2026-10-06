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


def test_installed_version_matches_importlib_metadata() -> None:
    assert entrypoint._read_installed_version() == version("unity-gateway")


def test_cli_import_defers_subcommand_specific_modules() -> None:
    _, loaded = _modules_loaded_after("import ucode.cli\nimport ucode.agents.claude", _LAZY_MODULES)

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
