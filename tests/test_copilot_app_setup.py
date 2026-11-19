"""``ug configure``'s GitHub Copilot desktop app setup: the login service note and the app's
providers, against a fake of the app's WebSocket API."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from tests.test_copilot_app import CATALOG, FakeApp
from ucode import agents as agents_mod
from ucode import copilot_app_setup, login_service, managed_files

WS = "https://example.cloud.databricks.com"


@pytest.fixture(autouse=True)
def _experiment(monkeypatch):
    monkeypatch.setenv("UG_EXPERIMENTAL_COPILOT_APP", "1")


@pytest.fixture(autouse=True)
def launches(monkeypatch):
    """No test starts a real app: on Linux without a display ug doesn't try, and ``_spawn`` is
    a mock."""
    spawn = Mock()
    monkeypatch.setattr(copilot_app_setup, "_spawn", spawn)
    monkeypatch.setattr(copilot_app_setup, "current_os", lambda: managed_files.OS.LINUX)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(copilot_app_setup, "LAUNCH_TIMEOUT_S", 0.3)
    monkeypatch.setattr(copilot_app_setup, "LAUNCH_POLL_S", 0.02)
    return spawn


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
    assert bases == {
        "Databricks Claude": "http://127.0.0.1:43199/copilot/anthropic",
        "Databricks GPT": "http://127.0.0.1:43199/copilot/responses/v1",
        "Databricks Open Models": "http://127.0.0.1:43199/copilot/completions/v1",
    }
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


@pytest.fixture
def closed_app(tmp_path, monkeypatch):
    """A closed app that ``_spawn`` can start; yields the started fakes."""
    home = tmp_path / ".copilot"
    monkeypatch.setenv("COPILOT_HOME", str(home))
    monkeypatch.setattr(copilot_app_setup, "installed", lambda: True)
    monkeypatch.setenv("DISPLAY", ":0")
    started: list[FakeApp] = []
    yield home, started
    for fake in started:
        fake.stop()


def test_configure_starts_a_closed_app_and_registers(service, closed_app, launches, capsys):
    home, started = closed_app
    launches.side_effect = lambda command: started.append(FakeApp(home))

    copilot_app_setup.configure(WS, CATALOG, provider_service=None)

    launches.assert_called_once_with(["/usr/bin/github"])
    assert len(started[0].providers) == 3
    out = " ".join(capsys.readouterr().out.split())
    assert "Starting the GitHub Copilot app" in out
    assert "added the Databricks models" in out


def test_an_app_that_doesnt_come_up_asks_to_run_configure_again(
    service, closed_app, launches, capsys
):
    copilot_app_setup.configure(WS, CATALOG, provider_service=None)

    launches.assert_called_once()
    assert login_service.copilot_app_recorded()
    out = " ".join(capsys.readouterr().out.split())
    assert "Open the GitHub Copilot app, then run `ug configure --agents copilot` again" in out


def test_an_app_that_fails_to_start_asks_to_run_configure_again(
    service, closed_app, launches, capsys
):
    launches.side_effect = OSError("no such file")

    copilot_app_setup.configure(WS, CATALOG, provider_service=None)

    out = " ".join(capsys.readouterr().out.split())
    assert "Open the GitHub Copilot app, then run `ug configure --agents copilot` again" in out


def test_linux_without_a_display_doesnt_start_the_app(service, closed_app, launches, monkeypatch):
    monkeypatch.delenv("DISPLAY")

    copilot_app_setup.configure(WS, CATALOG, provider_service=None)

    launches.assert_not_called()


def test_launch_commands(monkeypatch, tmp_path):
    monkeypatch.setattr(copilot_app_setup, "current_os", lambda: managed_files.OS.MACOS)
    assert copilot_app_setup._launch_command() == ["open", "-g", "-b", "com.github.githubapp"]

    monkeypatch.setattr(copilot_app_setup, "current_os", lambda: managed_files.OS.WINDOWS)
    monkeypatch.setattr(copilot_app_setup, "_windows_install_dir", lambda: str(tmp_path))
    assert copilot_app_setup._launch_command() is None
    (tmp_path / "github.exe").write_bytes(b"")
    assert copilot_app_setup._launch_command() == [str(tmp_path / "github.exe")]
    monkeypatch.setattr(copilot_app_setup, "_windows_install_dir", lambda: "")
    assert copilot_app_setup._launch_command() is None


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


def test_configure_skips_the_app_when_asked_or_not_installed(monkeypatch, launches):
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
    launches.assert_not_called()


def test_managed_config_turns_the_app_off(monkeypatch, capsys, launches):
    remove = Mock(return_value="removed")
    monkeypatch.setattr(copilot_app_setup, "remove", remove)
    monkeypatch.setattr(copilot_app_setup, "configure", Mock())

    agents_mod.configure_copilot_app(
        {"workspace": WS, "copilot_app_integration": False}, provider=None, parent_schema=None
    )

    remove.assert_called_once()
    launches.assert_not_called()
    assert "turns off the GitHub Copilot app" in " ".join(capsys.readouterr().out.split())


def test_without_the_experiment_flag_configure_and_revert_leave_the_app_alone(
    service, app, monkeypatch
):
    monkeypatch.delenv("UG_EXPERIMENTAL_COPILOT_APP")
    configure = Mock()
    monkeypatch.setattr(copilot_app_setup, "configure", configure)

    agents_mod.configure_copilot_app({"workspace": WS}, provider=None, parent_schema=None)

    configure.assert_not_called()
    assert copilot_app_setup.remove(keep_service=False) == "not set up"
    assert app.received == []
