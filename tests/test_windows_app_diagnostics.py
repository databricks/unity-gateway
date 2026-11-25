"""Evidence from a stalled Windows UI Automation client."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.utils.windows_app import WindowsApp


def test_uia_timeout_records_phase_and_still_fails(monkeypatch):
    evidence = []
    app = WindowsApp.__new__(WindowsApp)
    app.name = "chatgpt"
    app.powershell = Path("powershell.exe")
    app.proc = SimpleNamespace(pid=42, poll=lambda: None)
    app.session = SimpleNamespace(
        record=lambda name, value: evidence.append((name, value)), redact=lambda text: text
    )

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("powershell.exe", 60, stderr="uia phase=find-descendants\n")

    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(subprocess.TimeoutExpired):
        app.uia("tree")

    assert evidence == [
        (
            "chatgpt-uia-tree-timeout.json",
            {"command": "tree", "stderr": "uia phase=find-descendants\n"},
        )
    ]
