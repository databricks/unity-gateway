"""ug's login service unit: what the GitHub Copilot app's note changes in it."""

from __future__ import annotations

import plistlib
from unittest.mock import Mock

import pytest

from ucode import login_service, managed_files

WS = "https://example.cloud.databricks.com"


@pytest.fixture
def unit_env(tmp_path, monkeypatch):
    monkeypatch.setattr(login_service, "ug_binary", lambda: str(tmp_path / "ug"))
    monkeypatch.setattr(login_service.subprocess_cross_os, "run", Mock())
    monkeypatch.setattr(login_service.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    return login_service.service_path()


def _set_os(monkeypatch, target):
    monkeypatch.setattr(login_service, "current_os", lambda: target)


@pytest.mark.parametrize(
    "target", [managed_files.OS.LINUX, managed_files.OS.MACOS, managed_files.OS.WINDOWS]
)
def test_copilot_app_note_marks_the_unit_and_keeps_it_owned(unit_env, monkeypatch, target):
    _set_os(monkeypatch, target)
    login_service.install(WS, 43199)
    without = login_service.read_unit(unit_env)
    assert not login_service.copilot_app_recorded()

    login_service.install(WS, 43199, copilot_app=True)
    assert login_service.copilot_app_recorded()
    assert login_service.recorded_port("port") == 43199
    with_note = login_service.read_unit(unit_env)
    assert with_note.replace(f" {login_service.COPILOT_APP_NOTE}", "") == without

    login_service.install(WS, 43199)
    assert not login_service.copilot_app_recorded()
    assert login_service.read_unit(unit_env) == without


def test_codex_only_systemd_unit_is_unchanged(unit_env, monkeypatch):
    _set_os(monkeypatch, managed_files.OS.LINUX)
    login_service.install(WS, 43199, 43198)
    unit = unit_env.read_text().splitlines()
    assert unit[0] == "# Managed by unity-gateway; do not edit."
    assert unit[2].endswith(f'otel-proxy --host "{WS}" --relay-port 43198 --port 43199')


def test_launch_agent_keeps_its_definition(unit_env, monkeypatch):
    _set_os(monkeypatch, managed_files.OS.MACOS)
    login_service.install(WS, 43199, copilot_app=True)
    assert plistlib.loads(unit_env.read_bytes())["ProgramArguments"][1:] == [
        "otel-proxy",
        "--host",
        WS,
        "--port",
        "43199",
    ]
