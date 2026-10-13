"""Check packaged policy persistence as root/non-root in a disposable Docker container."""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if not Path("/.dockerenv").exists() or os.geteuid() != 0:
        raise SystemExit("Run only as root inside a disposable Docker container.")

    from ucode.managed_config import load_managed_configuration, save_managed_state
    from ucode.managed_files import UNITY_GATEWAY_MANAGED_SETTINGS_PATH, managed_file_fingerprint

    workspace = "https://policy-component.invalid"
    original = {"spec_version": 1, "default_agent": "CODING_AGENT_CLAUDE_CODE"}
    changed = {"spec_version": 1, "default_agent": "CODING_AGENT_CODEX"}
    policy = UNITY_GATEWAY_MANAGED_SETTINGS_PATH
    assert not policy.exists(), "The container must start without policy."
    save_managed_state(workspace, original, allow_sudo=False)
    assert policy.stat().st_uid == 0
    assert policy.stat().st_mode & 0o777 == 0o644
    assert policy.parent.stat().st_uid == 0
    assert policy.parent.stat().st_mode & 0o777 == 0o755
    assert load_managed_configuration(workspace) == original
    before = managed_file_fingerprint(policy)

    child_code = """
import os
import sys
from ucode.managed_config import load_managed_configuration, save_managed_state
from ucode.managed_files import ManagedFileWriteUnavailable
assert os.geteuid() == 10001
assert os.getgroups() == []
workspace = sys.argv[1]
original = {"spec_version": 1, "default_agent": "CODING_AGENT_CLAUDE_CODE"}
changed = {"spec_version": 1, "default_agent": "CODING_AGENT_CODEX"}
assert load_managed_configuration(workspace) == original
save_managed_state(workspace, original, interactive=sys.stdin.isatty(), allow_sudo=False)
for target_workspace, config in [(workspace, changed), (workspace, {}), ("https://other.invalid", changed)]:
    try:
        save_managed_state(target_workspace, config, interactive=sys.stdin.isatty(), allow_sudo=False)
    except ManagedFileWriteUnavailable as exc:
        assert "require an administrator or root provisioning run" in str(exc), str(exc)
    else:
        raise AssertionError("Non-root launch installed or removed policy")
assert load_managed_configuration(workspace) == original
print("PASS: non-root read, unchanged check, and deferred install/removal; tty=" + str(sys.stdin.isatty()))
"""
    command = [sys.executable, "-c", child_code, workspace]
    child_env = {**os.environ, "HOME": "/home/integration"}
    result = subprocess.run(
        command,
        user=10001,
        group=10001,
        extra_groups=[],
        env=child_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    print(result.stdout.strip())
    terminal_master, terminal_slave = pty.openpty()
    try:
        result = subprocess.run(
            command,
            user=10001,
            group=10001,
            extra_groups=[],
            env=child_env,
            stdin=terminal_slave,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert "tty=True" in result.stdout
        print(result.stdout.strip())
    finally:
        os.close(terminal_master)
        os.close(terminal_slave)
    assert managed_file_fingerprint(policy) == before
    assert json.loads(policy.read_text())["config"] == original
    foreign = "https://other.invalid"
    user_files = [
        Path(child_env["HOME"]) / ".ucode" / filename
        for filename in ("state.json", "refresh_ts.json")
    ]
    user_state = {path: path.read_bytes() if path.exists() else None for path in user_files}
    for tool in (None, "claude", "codex"):
        for dry_run in (False, True):
            args = [tool] if tool else []
            args += ["--workspace", foreign, "--skip-preflight"]
            if dry_run:
                args = ["--dry-run", *args]
            result = subprocess.run(
                ["ug", *args],
                user=10001,
                group=10001,
                extra_groups=[],
                env=child_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
            )
            output = " ".join((result.stdout + result.stderr).split())
            assert result.returncode == 1, output
            assert "Launch refused" in output, output
            assert workspace in output and foreign in output, output
            assert managed_file_fingerprint(policy) == before
            assert {
                path: path.read_bytes() if path.exists() else None for path in user_files
            } == user_state
    print("PASS: installed ug refuses foreign workspace for non-root bare/Claude/Codex launches")
    save_managed_state(workspace, changed, allow_sudo=False)
    assert load_managed_configuration(workspace) == changed
    assert policy.stat().st_uid == 0
    assert policy.stat().st_mode & 0o777 == 0o644
    save_managed_state(workspace, {}, allow_sudo=False)
    assert not policy.exists()
    print(
        "PASS: real root-owned 0644 policy/0755 directory, root install/update/removal without sudo"
    )


if __name__ == "__main__":
    main()
