"""Neutral process isolation and cleanup for installed-CLI tests."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from pathlib import Path


def process_group_options() -> dict:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


def clean_environment(home: Path) -> dict[str, str]:
    # An allowlist prevents a developer's agent keys, settings, plugins, Python
    # imports, and routing flags from silently changing the tested combination.
    keep = (
        "PATH",
        "SYSTEMROOT",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    )
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CODEX_HOME": str(home / ".codex"),
            "DATABRICKS_CONFIG_FILE": str(home / ".databrickscfg"),
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "160",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env


def stop_process(proc: subprocess.Popen) -> None:
    """Reap the entire process group, including servers left by a failed agent."""
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # The leader may have exited while a grandchild kept running.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    elif os.name == "nt" and proc.poll() is None:
        try:
            subprocess.run(
                [shutil.which("taskkill") or "taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.poll() is None:
            proc.kill()
    proc.wait(timeout=5)
