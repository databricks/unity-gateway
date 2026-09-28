"""`ug configure --file` CUJs: a local CodingAgentConfig drives the real configure path.

The file is the managed-config GET response shape and is applied as if that GET had returned it,
configuring every agent it enables through the real writers. Valid files are checked against the
effective generated settings; missing, malformed, and unsupported files must fail before anything
is written.
"""

import json
from pathlib import Path

import pytest
from utils.managed import (
    build_claude_agent_config,
    build_coding_agent_config,
    claude_picker,
    codex_listed,
    set_managed_config_stub,
    write_config_file,
)
from utils.terminal import TerminalProcess

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "managed_config"
# Differs from the model list the managed e2e workspace publishes, so only the file can produce it.
FILE_CLAUDE_MODELS = ["system.ai.claude-sonnet-4-6", "system.ai.claude-opus-4-8"]
FILE_CODEX_MODELS = ["system.ai.gpt-5-6-sol", "system.ai.gpt-5-4-nano"]
# Distinct from FILE_CLAUDE_MODELS so reconciliation to it is unambiguous.
FILE_CLAUDE_MODELS_CHANGED = ["system.ai.claude-haiku-4-5"]


def _sticky_note(path: Path) -> str:
    resolved = Path(path).expanduser().resolve()
    return (
        f"Using managed config from {resolved} (set with --file); "
        "run `ug configure` to use the workspace's config."
    )


def _compact(text: str) -> str:
    # Collapses whitespace so a hard line-wrap of the note's long path doesn't break the match.
    return "".join(text.split())


def _configure_with_file(session, workspace, path, *extra, ok=True):
    return session.run(
        "configure",
        "--file",
        str(path),
        "--workspace",
        workspace,
        *extra,
        "--skip-upgrade",
        timeout=240,
        ok=ok,
    )


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_ug_configure_file_valid_claude(live_session, workspace):
    """Scenario: ug configure --file with tests/fixtures/managed_config/claude.json.

    Expected: exit 0; Claude's generated picker is exactly the file's static model list.
    """
    _configure_with_file(live_session, workspace, FIXTURES / "claude.json")

    assert claude_picker(live_session) == FILE_CLAUDE_MODELS


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_ug_configure_file_valid_codex(live_session, workspace):
    """Scenario: ug configure --file with tests/fixtures/managed_config/claude_and_codex.json.

    Expected: exit 0; the generated Codex catalog lists exactly the file's static models.
    """
    _configure_with_file(live_session, workspace, FIXTURES / "claude_and_codex.json")

    assert codex_listed(live_session) == FILE_CODEX_MODELS


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_ug_configure_file_claude_launch_stays_sticky(live_session, workspace, tmp_path):
    """Scenario: configure --file claude.json, then a real plain Claude launch (no --file); change
    the file's model list, configure --file it again, then launch plainly once more.

    Expected: the first plain launch prints the sticky-file note and replays claude.json's model
    list with no re-fetch of the workspace's own (different) published list; after the file changes
    and is reapplied, the next plain launch's picker matches the new list instead.
    """
    session = live_session
    claude_path = FIXTURES / "claude.json"
    _configure_with_file(session, workspace, claude_path)

    command = [str(session.binary), "claude", "--", "--version"]
    with TerminalProcess(session, "claude", command, "configure-file-claude-launch") as terminal:
        terminal.finish(timeout=240)
    assert _compact(_sticky_note(claude_path)) in _compact(terminal.visible), terminal.visible
    assert claude_picker(session) == FILE_CLAUDE_MODELS

    changed_config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE", build_claude_agent_config(FILE_CLAUDE_MODELS_CHANGED)
    )
    changed_path = write_config_file(tmp_path, changed_config, "claude-changed.json")
    _configure_with_file(session, workspace, changed_path)

    with TerminalProcess(session, "claude", command, "configure-file-claude-relaunch") as terminal:
        terminal.finish(timeout=240)
    assert claude_picker(session) == FILE_CLAUDE_MODELS_CHANGED


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_ug_configure_file_codex_launch_stays_sticky(live_session, workspace):
    """Scenario: configure --file claude_and_codex.json, then a real plain Codex launch (no --file).

    Expected: the launch prints the sticky-file note and replays the file's Codex catalog with no
    re-fetch of the workspace's own published list.
    """
    session = live_session
    codex_path = FIXTURES / "claude_and_codex.json"
    _configure_with_file(session, workspace, codex_path)

    result = session.run("codex", "--", "--version", timeout=240)
    assert _compact(_sticky_note(codex_path)) in _compact(result.stdout), result.stdout
    assert codex_listed(session) == FILE_CODEX_MODELS


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_ug_configure_file_replaced_by_workspace_configure(live_session, workspace, tmp_path):
    """Scenario: configure --file claude.json, then a plain `configure` (no --file; the workspace's
    own publish is stood in for by a stub for a deterministic assertion), then a plain Claude launch.

    Expected: the plain configure force-refreshes and overwrites the sticky --file cache entry, so
    the following plain launch reflects the stub's model list, not claude.json's, and prints no
    sticky-file note.
    """
    session = live_session
    _configure_with_file(session, workspace, FIXTURES / "claude.json")

    workspace_models = ["system.ai.claude-sonnet-5"]
    workspace_config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE", build_claude_agent_config(workspace_models)
    )
    set_managed_config_stub(session, tmp_path, workspace_config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    command = [str(session.binary), "claude", "--", "--version"]
    with TerminalProcess(
        session, "claude", command, "configure-file-then-workspace-launch"
    ) as terminal:
        terminal.finish(timeout=240)
    assert claude_picker(session) == workspace_models
    assert _compact("Using managed config from") not in _compact(terminal.visible)


CLAUDE_JSON = (FIXTURES / "claude.json").read_text()
INVALID_INPUTS = {
    "missing_file": (None, (), "Cannot read --file"),
    "invalid_json": ("{ bad json }", (), "Invalid --file: expected valid JSON"),
    "unsupported_spec_version": (
        json.dumps({**json.loads(CLAUDE_JSON), "spec_version": 999}),
        (),
        "spec_version",
    ),
    "empty_enabled_agents": (
        '{"spec_version": 1, "enabled_agents": []}',
        (),
        "--file enables no known coding agents",
    ),
    "only_unknown_agent": (
        '{"spec_version": 1, "enabled_agents": [{"agent": "CODING_AGENT_NEXT", "config": {}}]}',
        (),
        "--file enables no known coding agents",
    ),
    "combined_with_agents": (
        CLAUDE_JSON,
        ("--agents", "claude"),
        "--file applies to every agent the file enables",
    ),
}


# Agent-marked so the per-agent managed CI lane selects it; the failure is agent-independent.
@pytest.mark.managed_fixture
@pytest.mark.claude
@pytest.mark.parametrize("case", INVALID_INPUTS, ids=list(INVALID_INPUTS))
def test_ug_configure_file_rejects_invalid_input(live_session, workspace, tmp_path, case):
    """Scenario: ug configure --file with a missing, malformed, or unsupported file, or with --agents.

    Expected: nonzero exit with the case's specific --file error; no agent settings written.
    """
    contents, extra, message = INVALID_INPUTS[case]
    path = Path(tmp_path) / f"{case}.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    result = _configure_with_file(live_session, workspace, path, *extra, ok=False)

    assert result.returncode != 0
    assert message in result.stderr
    assert not (live_session.home / ".claude" / "ucode-settings.json").exists()
    assert not (live_session.home / ".ucode" / "codex-model-catalog.json").exists()
