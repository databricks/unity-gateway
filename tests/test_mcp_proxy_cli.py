"""Component checks for the proxy's lightweight console-script dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
import typer
from typer.testing import CliRunner

from ucode.cli import mcp_proxy_cmd


@pytest.mark.parametrize("command", ["ug", "ucode"])
@pytest.mark.parametrize("workspace_id", [None, "123"])
def test_proxy_entrypoint_skips_agent_cli(tmp_path, command, workspace_id):
    state = tmp_path / ".ucode" / "state.json"
    state.parent.mkdir(exist_ok=True)
    saved = {
        "state_version": 3,
        "current_workspace": "https://saved.example",
        "workspaces": {"https://saved.example": {"profile": "saved", "use_pat": True}},
    }
    if workspace_id:
        key = f"https://saved.example?w={workspace_id}"
        saved["current_workspace_key"] = key
        saved["workspaces"][key] = {
            "profile": "saved",
            "use_pat": True,
            "workspace_id": workspace_id,
        }
    state.write_text(json.dumps(saved))
    probe = f"""
import sys, json
sys.argv = [{command!r}, "mcp-proxy", "--url", "https://endpoint.example/mcp"]
from ucode import mcp_proxy_runtime
calls = []
mcp_proxy_runtime.serve = lambda *args, **kwargs: calls.append([args, kwargs])
from ucode.entrypoint import main
try:
    main()
except SystemExit as exc:
    assert exc.code == 0
print(json.dumps({{"calls": calls, "loaded": [name for name in
    ("ucode.cli", "ucode.agents", "ucode.smart_routing.v2", "ucode.mcp", "typer", "mcp", "httpx", "ucode.state", "questionary", "ucode.ui")
    if name in sys.modules]}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "calls": [
            [
                ["https://endpoint.example/mcp", "https://saved.example", "saved"],
                {"use_pat": True, **({"workspace_id": workspace_id} if workspace_id else {})},
            ]
        ],
        "loaded": [],
    }


def test_proxy_explicit_workspace_and_profile(monkeypatch):
    from ucode import mcp_proxy_runtime

    calls = []
    monkeypatch.setattr(
        mcp_proxy_runtime,
        "load_proxy_state",
        lambda **_: {"workspace": "saved", "profile": "saved"},
    )
    monkeypatch.setattr(
        mcp_proxy_runtime, "serve", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    app = typer.Typer()
    app.command()(mcp_proxy_cmd)
    result = CliRunner().invoke(
        app,
        [
            "--url",
            "https://endpoint",
            "--host",
            "https://explicit",
            "--profile",
            "explicit",
            "--use-pat",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [(("https://endpoint", "https://explicit", "explicit"), {"use_pat": True})]


def test_proxy_missing_workspace_reports_error(monkeypatch):
    from ucode import mcp_proxy_runtime

    monkeypatch.setattr(mcp_proxy_runtime, "load_proxy_state", lambda **_: {})
    app = typer.Typer()
    app.command()(mcp_proxy_cmd)
    result = CliRunner().invoke(app, ["--url", "https://endpoint"])
    assert result.exit_code == 1
    assert "No workspace configured" in result.stderr
    assert result.stdout == ""
