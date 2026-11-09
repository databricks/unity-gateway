from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time

import pytest

from ucode import auth_token_log

WS = "https://example.cloud.databricks.com"


def _isolated_env(home, path: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATABRICKS_", "UCODE_"))
    }
    env.update(HOME=str(home), PATH=path)
    return env


class TestLogFailure:
    def test_appends_one_line_per_failure(self):
        auth_token_log.log_failure(WS, "prod", "first\n  failure")
        auth_token_log.log_failure(None, None, "second")
        lines = auth_token_log.LOG_PATH.read_text().splitlines()
        assert len(lines) == 2
        assert lines[0].endswith(f"host={WS} profile=prod first failure")
        assert lines[1].endswith("host=<unknown> profile=<none> second")

    def test_restarts_past_max_bytes(self, monkeypatch):
        monkeypatch.setattr(auth_token_log, "MAX_BYTES", 10)
        auth_token_log.log_failure(WS, None, "x" * 50)
        auth_token_log.log_failure(WS, None, "after")
        (line,) = auth_token_log.LOG_PATH.read_text().splitlines()
        assert line.endswith(" after")

    def test_unwritable_log_is_ignored(self, monkeypatch, tmp_path):
        not_a_dir = tmp_path / "file"
        not_a_dir.write_text("")
        monkeypatch.setattr(auth_token_log, "LOG_PATH", not_a_dir / "auth-token-errors.log")
        auth_token_log.log_failure(WS, None, "boom")  # must not raise


@pytest.mark.skipif(sys.platform == "win32", reason="Windows termination delivers no signal")
class TestKillLogging:
    def test_background_refresher_keeps_the_default_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        try:
            auth_token_log.log_if_killed(["--host", WS, "--refresh-cache"])
            assert signal.getsignal(signal.SIGTERM) is previous
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_sigterm_is_logged_and_still_kills(self, tmp_path):
        script = (
            "import time\n"
            "from ucode import auth_token_log\n"
            f"auth_token_log.log_if_killed(['--host', {WS!r}, '--profile', 'p'])\n"
            "auth_token_log.set_stage('loading the ug CLI')\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-P", "-c", script],
            env=_isolated_env(tmp_path, os.environ.get("PATH", "")),
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "ready"
            proc.send_signal(signal.SIGTERM)
            # Same exit status as an unhandled SIGTERM, so callers see no change.
            assert proc.wait(timeout=30) == -signal.SIGTERM
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
        log = (tmp_path / ".ucode" / "auth-token-errors.log").read_text()
        assert f"host={WS} profile=p killed by SIGTERM after" in log
        assert "while loading the ug CLI" in log

    def test_kill_while_waiting_on_the_cli_names_that_stage(self, tmp_path):
        """pi's timeout landing mid-`databricks auth token`, through the real entry point."""
        home, bin_dir = tmp_path / "home", tmp_path / "bin"
        home.mkdir()
        bin_dir.mkdir()
        started = tmp_path / "cli-pid"
        fake_cli = bin_dir / "databricks"
        fake_cli.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = --version ]; then echo "Databricks CLI v999.0.0"; exit 0; fi\n'
            f'echo $$ > "{started}.tmp" && mv "{started}.tmp" "{started}"\n'
            "exec sleep 60\n"
        )
        fake_cli.chmod(0o755)
        proc = subprocess.Popen(
            [sys.executable, "-P", "-m", "ucode", "auth-token", "--host", WS, "--profile", "p"],
            env=_isolated_env(home, f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 60
            while not started.exists():
                assert proc.poll() is None, "helper exited before it called the CLI"
                assert time.monotonic() < deadline, "helper never called the CLI"
                time.sleep(0.05)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=30) == -signal.SIGTERM
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if started.exists():
                with contextlib.suppress(OSError, ValueError):
                    os.kill(int(started.read_text()), signal.SIGKILL)
        log = (home / ".ucode" / "auth-token-errors.log").read_text()
        assert f"host={WS} profile=p killed by SIGTERM after" in log
        assert "while waiting for `databricks auth token`" in log


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_log_is_private():
    import stat

    auth_token_log.log_failure(WS, None, "boom")
    assert stat.S_IMODE(auth_token_log.LOG_PATH.stat().st_mode) == 0o600
