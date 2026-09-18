"""Version-floor gate: ug enforces minimum agent CLI versions at launch.

One scenario per agent, driven against the real installed CLI with a
below-floor pin (`--claude-version 2.1.258 --codex-version 0.144.0`, or the
matching workflow_dispatch inputs). The gate only exists below the floor, and
the suite forbids skips, so each scenario returns early when the pinned CLI
already meets the floor.
"""

from __future__ import annotations

import re
import subprocess
import time

import pytest
from utils.terminal import TerminalProcess

pytestmark = pytest.mark.installation

_FLOORS = {"claude": (2, 1, 259), "codex": (0, 145, 0)}
_REQUIREMENTS = {
    "claude": "ug requires Claude Code 2.1.259 or newer",
    "codex": "ug requires Codex 0.145.0 or newer",
}
_UPGRADE_PROMPTS = {
    "claude": "Upgrade Claude Code if available?",
    "codex": "Upgrade Codex if available?",
}
_UPGRADE_NOTES = {"claude": "Upgrading Claude Code", "codex": "Upgrading Codex"}
_UPGRADE_FAILURES = {
    "claude": "Could not update Claude Code",
    "codex": "Could not update Codex",
}
# Shown by the auto-configure flow once the launch clears the floor gate.
_PAST_GATE_SIGNALS = ("Loading Databricks workspaces", "Workspace URL", "Select workspace:")


def _installed_version(session, binary: str) -> tuple[int, int, int]:
    result = subprocess.run(
        [binary, "--version"],
        env=session.env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout + result.stderr)
    assert match, f"`{binary} --version` reported no semver: {result!r}"
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _wait_for_exit(tui: TerminalProcess, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while not tui.ended and time.monotonic() < deadline:
        tui.read()
    assert tui.ended, f"Process did not exit within {timeout}s:\n{tui.visible}"
    tui.child.close(force=False)


def _past_gate(text: str) -> bool:
    return any(signal in text for signal in _PAST_GATE_SIGNALS)


def _launch(session, agent: str, name: str) -> TerminalProcess:
    tui = TerminalProcess(session, agent, [str(session.binary), agent], name)
    tui.wait_for(lambda text: _UPGRADE_PROMPTS[agent] in text, "required-upgrade prompt")
    return tui


def _assert_gate_holds_after_failed_upgrade(tui: TerminalProcess, agent: str) -> None:
    """The floor clears only when the accepted upgrade actually succeeds.

    A working upgrader carries the launch past the gate to workspace
    configuration; a failed one (e.g. registry unreachable) must leave the
    launch blocked with the requirement restated.
    """
    tui.wait_for(
        lambda text: _past_gate(text) or _UPGRADE_FAILURES[agent] in text or tui.ended,
        "upgrade outcome",
        timeout=600,
    )
    transcript = "".join(tui.output)
    if _UPGRADE_FAILURES[agent] in transcript:
        _wait_for_exit(tui)
        assert _REQUIREMENTS[agent] in transcript
        assert tui.child.exitstatus != 0
    else:
        assert _past_gate(transcript), f"Launch stalled after upgrade:\n{tui.visible}"


def _check_version_floor(session, agent: str) -> None:
    if _installed_version(session, agent) >= _FLOORS[agent]:
        return  # The gate only exists below the floor; skips are forbidden here.
    with _launch(session, agent, "floor-decline") as tui:
        tui.send("n\r", "decline the required upgrade")
        _wait_for_exit(tui)
        transcript = "".join(tui.output)
        assert _REQUIREMENTS[agent] in transcript
        assert _UPGRADE_NOTES[agent] not in transcript
        assert tui.child.exitstatus != 0
    with _launch(session, agent, "floor-accept") as tui:
        tui.send("y\r", "accept the required upgrade")
        tui.wait_for(
            lambda text: _UPGRADE_NOTES[agent] in text, "native upgrade attempt", timeout=60
        )
        _assert_gate_holds_after_failed_upgrade(tui, agent)


@pytest.mark.claude
def test_claude_launch_below_version_floor(session):
    """Scenario: launch Claude Code pinned below the version floor.

    Expected: ug requires an upgrade first. Declining blocks the launch with the
    requirement restated; accepting runs the native upgrader and the floor
    clears only when the upgrade succeeds.
    """
    _check_version_floor(session, "claude")


@pytest.mark.codex
def test_codex_launch_below_version_floor(session):
    """Scenario: launch Codex pinned below the version floor.

    Expected: ug requires an upgrade first. Declining blocks the launch with the
    requirement restated; accepting runs the native upgrader and the floor
    clears only when the upgrade succeeds.
    """
    _check_version_floor(session, "codex")
