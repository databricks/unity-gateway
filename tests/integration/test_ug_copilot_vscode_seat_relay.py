"""CUJ: `ug configure` routes VS Code's Copilot runtime through the seat relay with ug's wrapper."""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.live, pytest.mark.copilot, pytest.mark.copilot_seat]

# Stands in for `code` with no VS Code running: records each call and keeps the package ug installs.
FAKE_CODE = """import os, shutil, sys
directory = os.environ["UG_TEST_CODE_DIR"]
with open(os.path.join(directory, "calls"), "a", encoding="utf-8") as calls:
    calls.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["--install-extension"]:
    shutil.copy(sys.argv[2], os.path.join(directory, "installed.vsix"))
"""
# Stands in for VS Code's bundled Copilot runtime: records how it was started, then exits.
FAKE_RUNTIME = """import json, os, sys
with open(os.environ["UG_TEST_RUNTIME_OUT"], "w", encoding="utf-8") as out:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ)}, out)
"""
_NODE_ARCHES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
WINDOWS_VERSION_FOLDER = "1234567890ab"


def _runtime_path(app: Path) -> Path:
    system = "win32" if os.name == "nt" else sys.platform
    target = f"{system}-{_NODE_ARCHES[platform.machine().lower()]}"
    name = "copilot-runtime.exe" if os.name == "nt" else "copilot-runtime"
    return (
        app
        / "node_modules.asar.unpacked/@github"
        / f"copilot-sdk-{target}/prebuilds"
        / target
        / name
    )


def _install_fake_vscode(root: Path) -> tuple[Path, Path]:
    """A VS Code install with a stand-in `code` and a bundled runtime where ug looks for them.
    Returns the bin directory and the runtime."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    code_script = root / "fake_code.py"
    code_script.write_text(FAKE_CODE, encoding="utf-8")
    runtime_script = root / "fake_runtime.py"
    runtime_script.write_text(FAKE_RUNTIME, encoding="utf-8")
    if os.name == "nt":
        # ug reads the version folder from the cli.js line of code.cmd; the stand-in keeps that
        # line as a comment and runs the recorder instead.
        (bin_dir / "code.cmd").write_text(
            "@echo off\r\n"
            f'rem "%~dp0..\\Code.exe" "%~dp0..\\{WINDOWS_VERSION_FOLDER}\\resources\\app\\out\\cli.js" %*\r\n'
            f'@"{sys.executable}" "{code_script}" %*\r\n',
            encoding="utf-8",
        )
        runtime = _runtime_path(root / WINDOWS_VERSION_FOLDER / "resources" / "app")
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b"")
        return bin_dir, runtime
    code = bin_dir / "code"
    code.write_text(
        f"#!/bin/sh\nexec '{sys.executable}' '{code_script}' \"$@\"\n", encoding="utf-8"
    )
    code.chmod(0o755)
    runtime = _runtime_path(root / "resources" / "app")
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        f"#!/bin/sh\nexec '{sys.executable}' '{runtime_script}' \"$@\"\n", encoding="utf-8"
    )
    runtime.chmod(0o755)
    return bin_dir, runtime


def _vscode_settings(session) -> Path:
    if os.name == "nt":
        return Path(session.env["APPDATA"]) / "Code/User/settings.json"
    if sys.platform == "darwin":
        return session.home / "Library/Application Support/Code/User/settings.json"
    return session.home / ".config/Code/User/settings.json"


def test_ug_configure_routes_vscode_copilot_through_the_seat_relay(
    live_session, workspace, copilot_seat_provider
):
    """Scenario: with a stand-in VS Code install on PATH (its `code` command and a bundled Copilot
    runtime where ug looks for them), run `ug configure --agents copilot --provider <seat relay>`.

    Expected: configure writes ug's runtime wrapper and points VS Code's Copilot runtime setting at
    it. On Linux, running that wrapper the way VS Code would starts `ug copilot-seat-runtime`,
    which starts the bundled runtime with its Copilot API URL on ug's loopback relay, loopback
    exempt from any proxy, and the WebSocket Responses transport off. On Windows the chain needs
    a real runtime executable, so only the files are checked.
    """
    session = live_session
    root = session.cwd / "fake-vscode"
    bin_dir, runtime = _install_fake_vscode(root)
    session.env["UG_TEST_CODE_DIR"] = str(bin_dir)
    session.env["PATH"] = f"{bin_dir}{os.pathsep}{session.env['PATH']}"

    result = session.run(
        "configure",
        "--agents",
        "copilot",
        "--provider",
        copilot_seat_provider,
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    output = " ".join(result.stdout.split())
    assert f"routing Copilot through {copilot_seat_provider}" in output, result.stdout
    wrappers = sorted((session.home / ".ucode/vscode/code").glob("ug-copilot-runtime*"))
    assert len(wrappers) == 1, wrappers
    wrapper = wrappers[0]
    settings = json.loads(_vscode_settings(session).read_text(encoding="utf-8"))
    assert settings["chat.agentHost.copilot.runtimePath"] == str(wrapper), settings
    session.record("vscode-seat-wrapper.json", {"wrapper": str(wrapper), "runtime": str(runtime)})
    if os.name == "nt":
        assert wrapper.suffix == ".js"
        return

    dump = session.cwd / "runtime-start.json"
    env = {**session.env, "UG_TEST_RUNTIME_OUT": str(dump)}
    started = subprocess.run(
        [str(wrapper), "--headless", "--stdio"],
        cwd=session.cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert started.returncode == 0, started.stdout + started.stderr
    recorded = json.loads(dump.read_text(encoding="utf-8"))
    session.record(
        "runtime-start.json", {"argv": recorded["argv"], "env_keys": sorted(recorded["env"])}
    )
    assert recorded["argv"][-2:] == ["--headless", "--stdio"], recorded["argv"]
    runtime_env = recorded["env"]
    assert runtime_env["COPILOT_API_URL"].startswith("http://127.0.0.1:"), runtime_env[
        "COPILOT_API_URL"
    ]
    assert "127.0.0.1" in runtime_env["NO_PROXY"], runtime_env["NO_PROXY"]
    assert runtime_env["COPILOT_CLI_DISABLE_WEBSOCKET_RESPONSES"] == "true"
