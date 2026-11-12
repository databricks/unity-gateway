"""CUJ: `ug configure` sets up Copilot Chat in VS Code so its first launch lists the models."""

import json
import os
import sys
import zipfile

import pytest

pytestmark = [pytest.mark.live, pytest.mark.copilot]

# Stands in for `code` with no VS Code running: records each call and keeps the package ug installs.
FAKE_CODE = """import os, shutil, sys
directory = os.environ["UG_TEST_CODE_DIR"]
with open(os.path.join(directory, "calls"), "a", encoding="utf-8") as calls:
    calls.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["--install-extension"]:
    shutil.copy(sys.argv[2], os.path.join(directory, "installed.vsix"))
"""


def _install_fake_code(directory):
    script = directory / "fake_code.py"
    script.write_text(FAKE_CODE, encoding="utf-8")
    if os.name == "nt":
        (directory / "code.cmd").write_text(
            f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
        return
    code = directory / "code"
    code.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
    code.chmod(0o755)


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_ug_configure_installs_an_extension_that_lists_models_in_an_open_window(
    live_session, workspace
):
    """Scenario: with a `code` command on PATH, run `ug configure --agents copilot`.

    Expected: configure installs the extension with the workspace's models, and that build asks
    VS Code to list its models once Copilot Chat is on (so a window open during configure shows
    them) and otherwise offers to turn Copilot Chat on. The next steps say to quit VS Code fully.
    """
    session = live_session
    code_dir = session.cwd / "fake-code"
    code_dir.mkdir()
    _install_fake_code(code_dir)
    session.env["UG_TEST_CODE_DIR"] = str(code_dir)
    session.env["PATH"] = f"{code_dir}{os.pathsep}{session.env['PATH']}"

    result = session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    output = _flat(result.stdout)
    assert "VS Code extension: installed databricks.unity-gateway" in output, result.stdout
    restart = (
        "Quit VS Code with Cmd+Q, even if no window is open"
        if sys.platform == "darwin"
        else "Close every VS Code window and open VS Code again."
    )
    assert restart in output, result.stdout
    assert "If VS Code is open" not in output, result.stdout
    with zipfile.ZipFile(code_dir / "installed.vsix") as vsix:
        config = json.loads(vsix.read("extension/ug-config.json"))
        bundle = vsix.read("extension/out/extension.js").decode()
    assert config["workspace"] == workspace.rstrip("/")
    assert config["models"], config
    assert "onDidChangeLanguageModelChatInformation" in bundle
    assert "workbench.action.chat.manage" in bundle
