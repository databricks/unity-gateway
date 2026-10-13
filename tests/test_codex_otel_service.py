"""Focused tests for the Linux Codex desktop OTEL user service."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ucode import codex_otel_service as service
from ucode import config_io

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux systemd user service")


def _state(**overrides: object) -> dict:
    state = {
        "workspace": "https://workspace.example",
        "profile": "default",
        "use_pat": False,
        "custom_oauth": None,
    }
    state.update(overrides)
    return state


@pytest.fixture
def isolated_service(tmp_path, monkeypatch):
    monkeypatch.setattr(config_io, "APP_DIR", tmp_path / "runtime")
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PATH", "/tmp/with space/$cli%bin:/usr/bin")
    monkeypatch.delenv("DATABRICKS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("DATABRICKS_AUTH_STORAGE", raising=False)
    monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
    monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
    monkeypatch.setattr(service, "ug_binary", lambda: "/opt/ug path/$bin%/ug")
    monkeypatch.setattr(
        service.shutil,
        "which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else "/tmp/databricks",
    )
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service.subprocess_cross_os, "run", run)
    return tmp_path, calls


def test_ensure_writes_quoted_unit_and_restarts_on_reconfigure(isolated_service, monkeypatch):
    tmp_path, calls = isolated_service
    monkeypatch.setattr(service, "_allocate_port", lambda: 34567)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", "~/.config/databricks.cfg")
    monkeypatch.setenv("DATABRICKS_AUTH_STORAGE", "plaintext")
    monkeypatch.setenv("DATABRICKS_BEARER", "secret-bearer")
    monkeypatch.setenv("DATABRICKS_BEARER_COMMAND", "/opt/secret-mint")
    state = _state()

    endpoint = service.ensure(state)
    assert endpoint == "http://127.0.0.1:34567/v1/traces"
    runtime = json.loads(service.runtime_path().read_text())
    assert runtime == {**state, "port": 34567}
    assert "token" not in service.runtime_path().read_text().lower()

    unit = service.unit_path().read_text()
    assert 'ExecStart="/opt/ug path/$$bin%%/ug" otel-proxy' in unit
    assert 'Environment="PATH=/tmp/with space/$cli%%bin:/usr/bin"' in unit
    assert f'Environment="DATABRICKS_CONFIG_FILE={home / ".config/databricks.cfg"}"' in unit
    assert 'Environment="DATABRICKS_AUTH_STORAGE=plaintext"' in unit
    assert "secret-bearer" not in unit
    assert "DATABRICKS_BEARER_COMMAND=" not in unit
    assert "Type=notify" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert "sh -c" not in unit
    assert calls == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "enable", service.SERVICE_NAME],
        ["/usr/bin/systemctl", "--user", "restart", service.SERVICE_NAME],
    ]

    # A repeated configure keeps the same files and port, and reconciles the
    # service manager by restarting the service.
    assert service.ensure(state) == endpoint
    assert calls[-3:] == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "enable", service.SERVICE_NAME],
        ["/usr/bin/systemctl", "--user", "restart", service.SERVICE_NAME],
    ]
    assert len(calls) == 6

    changed = service.ensure({**state, "use_pat": True})
    assert changed == endpoint
    assert calls[-1] == ["/usr/bin/systemctl", "--user", "restart", service.SERVICE_NAME]
    assert json.loads(service.runtime_path().read_text())["use_pat"] is True


def test_configured_endpoint_requires_a_valid_current_runtime_record(isolated_service, monkeypatch):
    _tmp_path, _calls = isolated_service
    service.runtime_path().parent.mkdir(parents=True)
    service.runtime_path().write_text(
        json.dumps({"workspace": "https://workspace.example", "port": 34567})
    )
    assert service.configured_endpoint() is None

    service.runtime_path().write_text(
        json.dumps(
            {
                "workspace": "https://workspace.example",
                "profile": None,
                "use_pat": False,
                "custom_oauth": None,
                "port": 34567,
            }
        )
    )
    assert service.configured_endpoint() == "http://127.0.0.1:34567/v1/traces"

    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    assert service.configured_endpoint() is None


def test_ensure_dry_run_does_not_install_or_start(isolated_service, monkeypatch):
    _, calls = isolated_service
    monkeypatch.setattr(service, "_allocate_port", lambda: 34567)
    monkeypatch.setattr(config_io, "_dry_run", True)

    assert service.ensure(_state()) == "http://127.0.0.1:34567/v1/traces"
    assert not service.unit_path().exists()
    assert not service.runtime_path().exists()
    assert calls == []


def test_remove_only_owned_files_and_honors_dry_run(isolated_service, monkeypatch):
    _tmp_path, calls = isolated_service
    monkeypatch.setattr(service, "_allocate_port", lambda: 34567)
    service.ensure(_state())
    calls.clear()

    config_io.set_dry_run(True)
    try:
        service.remove()
    finally:
        config_io.set_dry_run(False)
    assert service.unit_path().exists()
    assert service.runtime_path().exists()
    assert calls == []

    service.remove()
    assert not service.unit_path().exists()
    assert not service.runtime_path().exists()
    assert calls == [
        ["/usr/bin/systemctl", "--user", "disable", "--now", service.SERVICE_NAME],
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
    ]
    service.remove()
    assert len(calls) == 2


def test_remove_is_a_noop_off_linux(isolated_service, monkeypatch):
    _tmp_path, calls = isolated_service
    monkeypatch.setattr(service, "_allocate_port", lambda: 34567)
    service.ensure(_state())
    calls.clear()
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")

    service.remove()
    assert service.unit_path().exists()
    assert service.runtime_path().exists()
    assert calls == []


def test_initial_restart_failure_rolls_back_owned_files(isolated_service, monkeypatch):
    _tmp_path, calls = isolated_service
    monkeypatch.setattr(service, "_allocate_port", lambda: 34567)

    def run(command: list[str], **_kwargs):
        calls.append(command)
        if command[-2:] == ["restart", service.SERVICE_NAME]:
            return subprocess.CompletedProcess(command, 1, "", "restart failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service.subprocess_cross_os, "run", run)
    with pytest.raises(RuntimeError, match="restart failed"):
        service.ensure(_state())
    assert not service.unit_path().exists()
    assert not service.runtime_path().exists()
    assert calls[-2:] == [
        ["/usr/bin/systemctl", "--user", "disable", "--now", service.SERVICE_NAME],
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
    ]


def test_invalid_runtime_restarts_an_existing_owned_unit(isolated_service, monkeypatch):
    _tmp_path, calls = isolated_service
    monkeypatch.setattr(service, "_allocate_port", lambda: 34567)
    service.ensure(_state())
    calls.clear()
    service.runtime_path().write_text("not json")

    service.ensure(_state())
    assert calls[-1] == ["/usr/bin/systemctl", "--user", "restart", service.SERVICE_NAME]


@pytest.mark.parametrize("failed_operation", ["daemon-reload", "enable", "restart"])
def test_ensure_retries_systemd_reconciliation_after_failure(
    isolated_service, monkeypatch, failed_operation
):
    _tmp_path, calls = isolated_service
    monkeypatch.setattr(service, "_allocate_port", lambda: 34567)
    service.ensure(_state())
    calls.clear()
    monkeypatch.setenv("PATH", "/tmp/updated/bin")
    changed = _state(use_pat=True)
    failed = True

    def run(command: list[str], **_kwargs):
        nonlocal failed
        calls.append(command)
        if failed and (
            command[-1] == failed_operation
            or command[-2:] == [failed_operation, service.SERVICE_NAME]
        ):
            failed = False
            return subprocess.CompletedProcess(command, 1, "", f"{failed_operation} failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service.subprocess_cross_os, "run", run)
    with pytest.raises(RuntimeError, match=f"{failed_operation} failed"):
        service.ensure(changed)
    assert json.loads(service.runtime_path().read_text())["use_pat"] is True
    assert 'Environment="PATH=/tmp/updated/bin"' in service.unit_path().read_text()
    expected_calls = [["/usr/bin/systemctl", "--user", "daemon-reload"]]
    if failed_operation in {"enable", "restart"}:
        expected_calls += [
            ["/usr/bin/systemctl", "--user", "enable", service.SERVICE_NAME],
        ]
    if failed_operation == "restart":
        expected_calls.append(["/usr/bin/systemctl", "--user", "restart", service.SERVICE_NAME])
    assert calls == expected_calls

    calls.clear()
    service.ensure(changed)
    assert calls == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "enable", service.SERVICE_NAME],
        ["/usr/bin/systemctl", "--user", "restart", service.SERVICE_NAME],
    ]


def test_otel_proxy_subprocess_notifies_ready_and_releases_port(monkeypatch, tmp_path):
    home = tmp_path / "home"
    app_dir = home / ".ucode"
    app_dir.mkdir(parents=True)
    monkeypatch.setattr(config_io, "APP_DIR", app_dir)
    monkeypatch.setenv("HOME", str(home))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    service.runtime_path().write_text(
        json.dumps(
            {
                "workspace": "https://workspace.example",
                "profile": None,
                "use_pat": False,
                "custom_oauth": None,
                "port": port,
            }
        )
    )

    notify_path = tmp_path / "notify.sock"
    notify_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify_socket.bind(str(notify_path))
    executable = Path(sys.executable).with_name("ug")
    if not executable.is_file():
        executable = Path(shutil.which("ug") or "ug")
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "NOTIFY_SOCKET": str(notify_path),
            "DATABRICKS_BEARER": "synthetic-bearer",
            "DATABRICKS_CONFIG_FILE": str(tmp_path / "missing-databrickscfg"),
            "DATABRICKS_AUTH_STORAGE": "plaintext",
        }
    )
    environment.pop("DATABRICKS_BEARER_COMMAND", None)
    process = subprocess.Popen(
        [str(executable), "otel-proxy"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        notify_socket.settimeout(10)
        message, _address = notify_socket.recvfrom(1024)
        assert message == b"READY=1"

        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass

        process.send_signal(signal.SIGTERM)
        returncode = process.wait(timeout=10)
        _stdout, stderr = process.communicate(timeout=5)
        assert returncode == 0, stderr

        deadline = time.monotonic() + 5
        while True:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as released:
                try:
                    released.bind(("127.0.0.1", port))
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                else:
                    break
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.communicate(timeout=5)
        notify_socket.close()
