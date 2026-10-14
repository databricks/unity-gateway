from pathlib import Path

import pytest

from scripts import run_integration as runner


def windows_live_environment():
    return {"DATABRICKS_BEARER": "bearer"}


def test_secrets_are_redacted_from_build_output():
    output = "failed for bearer-a with token-b and token-c"

    assert (
        runner.redact_secrets(output, ("bearer-a", "token-b", "token-c"))
        == "failed for <redacted> with <redacted> and <redacted>"
    )


def test_headless_only_selects_exact_nodes_for_requested_agents(tmp_path):
    assert runner.integration_test_targets(
        tmp_path,
        ["claude", "codex"],
        platform_name="nt",
        installation_only=False,
        headless_only=True,
    ) == [
        f"{tmp_path / 'test_ug_claude_headless.py'}::test_ug_claude_headless_prompt_argument",
        f"{tmp_path / 'test_ug_codex_headless.py'}::test_ug_codex_headless_prompt_argument",
    ]


def test_windows_live_runs_select_only_modules_without_pty_helpers(tmp_path):
    (tmp_path / "test_headless.py").write_text("import json\n")
    (tmp_path / "test_tui.py").write_text("from utils.terminal import AgentTerminal\n")
    (tmp_path / "test_tui_import.py").write_text("from utils import terminal as agent_terminal\n")
    (tmp_path / "test_relative_mcp.py").write_text("from .utils import mcp as mcp_tools\n")
    (tmp_path / "test_qualified_tui.py").write_text("import tests.integration.utils.terminal\n")
    (tmp_path / "test_mcp.py").write_text("from utils.mcp import inventory\n")
    (tmp_path / "test_screen.py").write_text("import pyte\n")
    (tmp_path / "test_ug_claude_tracing.py").write_text("import json\n")

    assert runner.integration_test_targets(
        tmp_path,
        ["claude"],
        platform_name="nt",
        installation_only=False,
        headless_only=False,
    ) == [str(tmp_path / "test_headless.py")]
    assert runner.integration_test_targets(
        tmp_path,
        ["claude"],
        platform_name="posix",
        installation_only=False,
        headless_only=False,
    ) == [str(tmp_path)]


def test_headless_only_is_mutually_exclusive_with_installation_only():
    with pytest.raises(SystemExit):
        runner.arguments(
            [
                "--claude-version",
                "2.1.268",
                "--installation-only",
                "--headless-only",
            ],
            platform_name="nt",
            environment={},
        )


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [
        (["--headless-only"], windows_live_environment()),
        (["--headless-only", "--workspace", "https://example.test"], {}),
    ],
)
def test_headless_only_requires_live_workspace_and_auth(arguments, environment):
    with pytest.raises(SystemExit):
        runner.arguments(
            ["--claude-version", "2.1.268", *arguments],
            platform_name="nt",
            environment=environment,
        )


def test_headless_only_is_allowed_on_windows_with_live_workspace_and_auth():
    args = runner.arguments(
        [
            "--claude-version",
            "2.1.268",
            "--headless-only",
            "--workspace",
            "https://example.test",
        ],
        platform_name="nt",
        environment=windows_live_environment(),
    )

    assert args.headless_only is True
    assert args.installation_only is False
    assert args.pytest_args == ["-m", "live"]


def test_windows_policy_paths_match_pinned_agent_locations():
    paths = runner.managed_policy_paths(
        ["claude", "codex"],
        {"PROGRAMDATA": "C:/ProgramData", "PROGRAMFILES": "C:/Program Files"},
        platform_name="nt",
        system_platform="win32",
    )

    assert paths == (
        Path("C:/ProgramData/OpenAI/Codex/requirements.toml"),
        Path("C:/ProgramData/OpenAI/Codex/config.toml"),
        Path("C:/Program Files/ClaudeCode/managed-settings.json"),
    )


def test_windows_policy_preflight_fails_closed_without_required_roots():
    with pytest.raises(RuntimeError, match="PROGRAMDATA"):
        runner.managed_policy_paths(["codex"], {}, platform_name="nt", system_platform="win32")
