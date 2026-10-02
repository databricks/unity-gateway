"""Offline checks for neutral mechanics shared by both installed-CLI suites."""

import subprocess
import sys

from tests.e2e_helpers.process import clean_environment, process_group_options, stop_process
from tests.integration.utils import harness


def test_integration_reuses_neutral_process_helpers():
    assert harness.clean_environment is clean_environment
    assert harness.process_group_options is process_group_options
    assert harness.stop_process is stop_process


def test_clean_environment_isolates_home_and_drops_credentials(tmp_path, monkeypatch):
    for key in (
        "UG_CUJ_SP_CLIENT_ID",
        "UG_CUJ_SP_CLIENT_SECRET",
        "DATABRICKS_BEARER",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "UCODE_MANAGED_CONFIG_STUB",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "must-not-be-inherited")
    env = clean_environment(tmp_path)
    assert "must-not-be-inherited" not in env.values()
    assert env["HOME"] == str(tmp_path)
    assert env["CODEX_HOME"] == str(tmp_path / ".codex")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude")


def test_stop_process_reaps_a_real_child():
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **process_group_options(),
    )
    try:
        stop_process(proc)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
