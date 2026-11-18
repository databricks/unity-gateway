"""``ug configure``'s GitHub Copilot desktop app setup: the login service note and the app's
providers, against a fake of the app's WebSocket API."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from tests.test_copilot_app import CATALOG, FakeApp
from ucode import agents as agents_mod
from ucode import copilot_app_setup, login_service, managed_files

WS = "https://example.cloud.databricks.com"


@pytest.fixture
def service(tmp_path, monkeypatch):
    run = Mock()
    monkeypatch.setattr(login_service, "current_os", lambda: managed_files.OS.LINUX)
    monkeypatch.setattr(login_service, "ug_binary", lambda: str(tmp_path / "ug"))
    monkeypatch.setattr(login_service.subprocess_cross_os, "run", run)
    monkeypatch.setattr(login_service, "allocate_port", lambda: 43199)
    return run


@pytest.fixture
def app(tmp_path, monkeypatch):
    home = tmp_path / ".copilot"
    monkeypatch.setenv("COPILOT_HOME", str(home))
    monkeypatch.setattr(copilot_app_setup, "installed", lambda: True)
    fake = FakeApp(home)
    yield fake
    fake.stop()


def _systemctl(run):
    return [call.args[0][2:] for call in run.call_args_list]


def test_configure_keeps_the_service_for_the_app_and_registers_its_port(service, app, capsys):
    copilot_app_setup.configure(WS, CATALOG, provider_service=None)

    assert login_service.copilot_app_recorded()
    assert login_service.recorded_port("port") == 43199
    assert login_service.recorded_workspace() == (WS, None)
    bases = {p["name"]: p["settings"]["baseUrl"] for p in app.providers.values()}
    assert bases["Databricks Claude"] == "http://127.0.0.1:43199/copilot/anthropic"
    assert "added the Databricks models" in capsys.readouterr().out
    assert copilot_app_setup.status() == "set up (login service port 43199)"

    # Configuring again restarts the unchanged service so it reads the new settings.
    copilot_app_setup.configure(WS, CATALOG, provider_service=None)
    assert ["restart", login_service.SERVICE_NAME] in _systemctl(service)


def test_a_closed_app_keeps_the_service_and_asks_to_run_configure_again(
    service, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "none"))

    copilot_app_setup.configure(WS, CATALOG, provider_service=None)

    assert login_service.copilot_app_recorded()
    out = " ".join(capsys.readouterr().out.split())
    assert "Open the GitHub Copilot app, then run `ug configure --agents copilot` again" in out


def test_remove_deletes_the_providers_and_releases_the_service(service, app):
    copilot_app_setup.configure(WS, CATALOG, provider_service=None)

    assert copilot_app_setup.remove(keep_service=True) == "removed"
    assert app.providers == {}
    assert login_service.service_path().exists()
    assert not login_service.copilot_app_recorded()

    copilot_app_setup.configure(WS, CATALOG, provider_service=None)
    assert copilot_app_setup.remove(keep_service=False) == "removed"
    assert not login_service.service_path().exists()


def test_remove_without_a_setup_touches_nothing(service, monkeypatch):
    monkeypatch.setattr(copilot_app_setup, "installed", lambda: False)

    assert copilot_app_setup.remove(keep_service=False) == "not set up"
    assert service.call_args_list == []


def test_a_closed_app_on_remove_says_what_to_delete(service, app, monkeypatch):
    copilot_app_setup.configure(WS, CATALOG, provider_service=None)
    monkeypatch.setenv("COPILOT_HOME", "/nonexistent")

    assert copilot_app_setup.remove(keep_service=False).startswith("app closed")
    assert not login_service.service_path().exists()


def test_configure_skips_the_app_when_asked_or_not_installed(monkeypatch):
    configure = Mock()
    monkeypatch.setattr(copilot_app_setup, "configure", configure)
    state = {"workspace": WS}

    agents_mod.request_copilot_app_with_copilot(False)
    try:
        monkeypatch.setattr(copilot_app_setup, "installed", lambda: True)
        agents_mod.configure_copilot_app(state, provider=None, parent_schema=None)
    finally:
        agents_mod.request_copilot_app_with_copilot(True)
    monkeypatch.setattr(copilot_app_setup, "installed", lambda: False)
    agents_mod.configure_copilot_app(state, provider=None, parent_schema=None)

    configure.assert_not_called()


def test_managed_config_turns_the_app_off(monkeypatch, capsys):
    remove = Mock(return_value="removed")
    monkeypatch.setattr(copilot_app_setup, "remove", remove)
    monkeypatch.setattr(copilot_app_setup, "configure", Mock())

    agents_mod.configure_copilot_app(
        {"workspace": WS, "copilot_app_integration": False}, provider=None, parent_schema=None
    )

    remove.assert_called_once()
    assert "turns off the GitHub Copilot app" in " ".join(capsys.readouterr().out.split())
