"""Version-floor gate: below-floor agent CLIs hit the required-upgrade prompt at launch.

These scenarios exercise the launch-time minimum-version floor end to end. They
only do meaningful work when the runner pins a below-floor agent version, e.g.
`--claude-version 2.1.258 --codex-version 0.144.0` (CI: the workflow_dispatch
`claude_version` / `codex_version` inputs). With an at-floor-or-newer agent the
floor never engages and the scenario skips.
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
_PAST_GATE_SIGNALS = ("Loading Databricks workspaces", "Workspace URL")


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


def _require_below_floor(session, agent: str) -> None:
    version = _installed_version(session, agent)
    floor = _FLOORS[agent]
    if version >= floor:
        pytest.skip(
            f"{agent} {'.'.join(map(str, version))} meets the floor; "
            "pin a below-floor version to exercise the gate"
        )


def _wait_for_exit(tui: TerminalProcess, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while not tui.ended and time.monotonic() < deadline:
        tui.read()
    assert tui.ended, f"Process did not exit within {timeout}s:\n{tui.visible}"
    tui.child.close(force=False)


def _spawn_launch(session, agent: str, name: str) -> TerminalProcess:
    tui = TerminalProcess(session, agent, [str(session.binary), agent], name)
    tui.wait_for(lambda text: _UPGRADE_PROMPTS[agent] in text, "required-upgrade prompt")
    return tui


def _assert_accept_outcome(tui: TerminalProcess, agent: str) -> None:
    """After acceptance, the upgrader runs and the floor clears only on success.

    A working upgrader carries the launch past the gate to workspace
    configuration; a failed one (e.g. registry unreachable) must leave the
    launch blocked with the requirement restated.
    """

    def past_gate(text):
        return any(signal in text for signal in _PAST_GATE_SIGNALS)

    def failed(text):
        return _UPGRADE_FAILURES[agent] in text

    tui.wait_for(
        lambda text: past_gate(text) or failed(text) or tui.ended,
        "upgrade outcome",
        timeout=600,
    )
    transcript = "".join(tui.output)
    if failed(transcript):
        _wait_for_exit(tui)
        assert _REQUIREMENTS[agent] in transcript
        assert tui.child.exitstatus != 0
    else:
        assert past_gate(transcript), f"Launch stalled after upgrade:\n{tui.visible}"


@pytest.mark.claude
def test_below_floor_claude_decline_blocks_launch(session):
    """Scenario: launch Claude Code pinned below the version floor and decline the upgrade.

    Expected: the requirement warning and upgrade prompt appear, no upgrade is
    attempted, and the launch exits nonzero without reaching the agent.
    """
    _require_below_floor(session, "claude")
    with _spawn_launch(session, "claude", "floor-decline") as tui:
        tui.send("n\r", "decline the required upgrade")
        _wait_for_exit(tui)
        transcript = "".join(tui.output)
        assert _REQUIREMENTS["claude"] in transcript
        assert _UPGRADE_NOTES["claude"] not in transcript
        assert tui.child.exitstatus != 0


@pytest.mark.claude
def test_below_floor_claude_accept_upgrades_past_the_gate(session):
    """Scenario: launch Claude Code pinned below the floor and accept the upgrade.

    Expected: the native upgrader runs; a successful upgrade carries the launch
    past the gate to workspace configuration, while a failed one leaves the
    launch blocked with the requirement restated.
    """
    _require_below_floor(session, "claude")
    with _spawn_launch(session, "claude", "floor-accept") as tui:
        tui.send("y\r", "accept the required upgrade")
        tui.wait_for(
            lambda text: _UPGRADE_NOTES["claude"] in text, "native upgrade attempt", timeout=60
        )
        _assert_accept_outcome(tui, "claude")


@pytest.mark.codex
def test_below_floor_codex_decline_blocks_launch(session):
    """Scenario: launch Codex pinned below the version floor and decline the upgrade.

    Expected: the requirement warning and upgrade prompt appear, no upgrade is
    attempted, and the launch exits nonzero without reaching the agent.
    """
    _require_below_floor(session, "codex")
    with _spawn_launch(session, "codex", "floor-decline") as tui:
        tui.send("n\r", "decline the required upgrade")
        _wait_for_exit(tui)
        transcript = "".join(tui.output)
        assert _REQUIREMENTS["codex"] in transcript
        assert _UPGRADE_NOTES["codex"] not in transcript
        assert tui.child.exitstatus != 0


@pytest.mark.codex
def test_below_floor_codex_accept_upgrades_past_the_gate(session):
    """Scenario: launch Codex pinned below the floor and accept the upgrade.

    Expected: the native upgrader runs; a successful upgrade carries the launch
    past the gate to workspace configuration, while a failed one leaves the
    launch blocked with the requirement restated.
    """
    _require_below_floor(session, "codex")
    with _spawn_launch(session, "codex", "floor-accept") as tui:
        tui.send("y\r", "accept the required upgrade")
        tui.wait_for(
            lambda text: _UPGRADE_NOTES["codex"] in text, "native upgrade attempt", timeout=60
        )
        _assert_accept_outcome(tui, "codex")
