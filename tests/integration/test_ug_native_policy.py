"""Native settings and required policy through installed ug and real agents."""

import json
import sys
import tomllib
from pathlib import Path

import pytest
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL
from utils.evidence import FileTask
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
)
from utils.terminal import AgentTerminal, TerminalProcess


@pytest.mark.installation
def test_ug_and_ucode_reject_invalid_native_policy_before_setup(session):
    """Scenario: both installed entry points receive invalid native configuration.

    Expected: unsupported env/MCP fields, invalid requirements, conflicting exporter
    variants, and incompatible org pins fail before setup without leaking values.
    Codex app-server stdout remains empty and no agent configuration is created.
    """
    source = session.cwd / "invalid native.json"
    sentinel = "native-value-must-not-appear"
    for agent, extension in (
        ("claude", {"native_settings": {"env": {"PRIVATE_VALUE": sentinel}}}),
        ("claude", {"native_settings": {"managedMcpServers": {"private": sentinel}}}),
        ("claude", {"native_settings": {"forceLoginOrgUUID": sentinel}}),
        ("codex", {"native_settings": {"mcp_servers": {"private": sentinel}}}),
        ("codex", {"native_requirements": {"features": {"fast_mode": True}}}),
        (
            "codex",
            {
                "native_settings": {
                    "otel": {
                        "trace_exporter": {
                            "otlp-http": {"endpoint": sentinel, "protocol": "json"},
                            "otlp-grpc": {"endpoint": sentinel},
                        }
                    }
                }
            },
        ),
    ):
        entry = (
            build_claude_agent_config([CLAUDE_TEST_MODEL])
            if agent == "claude"
            else build_codex_agent_config(models=[CODEX_TEST_MODEL])
        )
        entry["config"].update(extension)
        source.write_text(json.dumps(build_coding_agent_config(entry["agent"], entry)))
        for executable in ("ug", "ucode"):
            result = session.run(
                agent,
                "--workspace",
                "https://workspace.example.invalid",
                "-f",
                source.name,
                *(["app-server", "--listen", "stdio://"] if agent == "codex" else []),
                binary=session.binary.with_name(executable),
                ok=False,
                timeout=30,
                strip_ansi=False,
            )
            assert result.returncode != 0
            assert "native_" in result.stdout + result.stderr
            assert "Traceback" not in result.stdout + result.stderr
            assert sentinel not in result.stdout + result.stderr
            if agent == "codex":
                assert result.stdout == ""
            assert not (session.home / ".ucode").exists()
            assert not (session.home / ".claude").exists()
            assert not (session.home / ".codex").exists()


@pytest.mark.live
@pytest.mark.tui
@pytest.mark.claude
def test_ug_claude_native_hook_policy_and_omission(live_session, workspace):
    """Scenario: launch with native Claude policy and later omit the native declarations.

    Expected: the native SessionStart hook runs and a real TUI file task completes.
    Both private and OS settings contain the declared policy/helper. Omission removes
    owned policy and stops the hook while preserving an unrelated same-matcher hook.
    """
    session = live_session
    private = session.home / ".claude/ucode-settings.json"
    private.parent.mkdir()
    retained_hook = {
        "type": "command",
        "command": "echo retained >> retained-native-hook.txt",
    }
    private.write_text(
        json.dumps(
            {
                "hooks": {"SessionStart": [{"hooks": [retained_hook]}]},
                "permissions": {"allow": ["Glob"]},
            }
        )
    )
    entry = build_claude_agent_config([CLAUDE_TEST_MODEL])
    native_hook = {"type": "command", "command": "echo owned >> owned-native-hook.txt"}
    entry["config"]["native_settings"] = {
        "hooks": {"SessionStart": [{"hooks": [native_hook]}]},
        "permissions": {"allow": ["Read"]},
        "sandbox": {"enabled": True},
        "allowManagedPermissionRulesOnly": True,
        "disableWorkflows": True,
        "otelHeadersHelper": 'printf \'{"x-native-fixture":"telemetry"}\'',
    }
    source = session.cwd / "native policy.json"
    source.write_text(json.dumps(build_coding_agent_config(entry["agent"], entry)))
    command = [str(session.binary), "claude", "--workspace", workspace, "-f", str(source)]
    task = FileTask(session)
    with AgentTerminal(session, "claude", command, "native-policy-task") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    owned_marker = session.cwd / "owned-native-hook.txt"
    retained_marker = session.cwd / "retained-native-hook.txt"
    assert set(owned_marker.read_text().splitlines()) == {"owned"}
    assert set(retained_marker.read_text().splitlines()) == {"retained"}
    managed = Path(
        "/Library/Application Support/ClaudeCode/managed-settings.json"
        if sys.platform == "darwin"
        else "/etc/claude-code/managed-settings.json"
    )
    for path in (private, managed):
        actual = json.loads(path.read_text())
        assert actual["sandbox"]["enabled"] is True
        assert actual["allowManagedPermissionRulesOnly"] is True
        assert actual["disableWorkflows"] is True
        assert (
            actual["otelHeadersHelper"] == entry["config"]["native_settings"]["otelHeadersHelper"]
        )
        assert "Read" in actual["permissions"]["allow"]
        assert native_hook in [
            hook for group in actual["hooks"]["SessionStart"] for hook in group["hooks"]
        ]

    before = (owned_marker.read_bytes(), retained_marker.read_bytes())
    del entry["config"]["native_settings"]
    source.write_text(json.dumps(build_coding_agent_config(entry["agent"], entry)))
    with AgentTerminal(session, "claude", command, "native-policy-omitted") as tui:
        tui.boot()
        tui.check_input_and_exit()
    assert owned_marker.read_bytes() == before[0]
    assert len(retained_marker.read_bytes()) > len(before[1])
    for path in (private, managed):
        actual = json.loads(path.read_text())
        assert "sandbox" not in actual
        assert "allowManagedPermissionRulesOnly" not in actual
        assert "disableWorkflows" not in actual
        assert "otelHeadersHelper" not in actual
        assert "Read" not in actual.get("permissions", {}).get("allow", [])
    actual_private = json.loads(private.read_text())
    assert actual_private["permissions"]["allow"] == ["Glob"]
    assert retained_hook in [
        hook for group in actual_private["hooks"]["SessionStart"] for hook in group["hooks"]
    ]


@pytest.mark.live
@pytest.mark.tui
@pytest.mark.codex
def test_ug_codex_native_requirements_enforced_and_released(live_session, workspace):
    """Scenario: launch Codex with native settings and fast-mode requirements, then release.

    Expected: a real TUI file task completes; app-server reports native telemetry
    and loaded requirements; required false wins over a bare Codex feature override.
    Release removes both managed destinations' owned fields, preserving a private sibling
    and allowing the same feature override to take effect again.
    """
    session = live_session
    private = session.home / ".codex/ucode.config.toml"
    private.parent.mkdir()
    private.write_text("[notice]\nhide_rate_limit_model_nudge = true\n")
    entry = build_codex_agent_config(models=[CODEX_TEST_MODEL])
    native = {
        "otel": {
            "environment": "ug-native-fixture",
            "log_user_prompt": False,
            "exporter": "none",
            "metrics_exporter": "none",
            "trace_exporter": "none",
        },
        "features": {"hooks": False},
        "tui": {"status_line": ["model-name"]},
        "model_auto_compact_token_limit": 100000,
    }
    entry["config"]["native_settings"] = native
    entry["config"]["native_requirements"] = {"features": {"fast_mode": False}}
    config = build_coding_agent_config(entry["agent"], entry)
    config["handoff"] = {
        "schema_version": 1,
        "owner": "native-fixture",
        "migration_version": 1,
        "agents": {"codex": {}},
    }
    source = session.cwd / "native policy.json"
    source.write_text(json.dumps(config))
    args = ["--workspace", workspace, "-f", str(source)]
    task = FileTask(session)
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex", *args], "native-policy-task"
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "codex")
    managed = Path("/etc/codex/managed_config.toml")
    requirements = Path("/etc/codex/requirements.toml")
    for path in (private, managed):
        actual = tomllib.loads(path.read_text())
        for key, value in native.items():
            assert actual[key] == value
    assert tomllib.loads(requirements.read_text())["features"]["fast_mode"] is False
    server_args = [*args, "app-server", "--listen", "stdio://"]
    response = session.app_server_handshake(
        server_args,
        request=("configRequirements/read", None),
        name="native-requirements-read",
    )
    assert response["result"]["requirements"]["featureRequirements"]["fast_mode"] is False
    effective = session.app_server_handshake(
        server_args,
        request=("config/read", {"includeLayers": False}),
        name="native-config-read",
    )["result"]["config"]
    for key, value in native["otel"].items():
        assert effective["otel"][key] == value
    assert effective["tui"]["status_line"] == ["model-name"]
    assert effective["model_auto_compact_token_limit"] == 100000
    override = session.run(
        "-c",
        "features.fast_mode=true",
        "features",
        "list",
        binary="codex",
        timeout=30,
    )
    feature_rows = [
        line.split() for line in override.stdout.splitlines() if line.startswith("fast_mode ")
    ]
    assert feature_rows == [["fast_mode", "stable", "false"]], override.stdout
    release = [
        str(session.binary),
        "managed-config",
        "release",
        "--owner",
        "native-fixture",
        "--agent",
        "codex",
    ]
    for phase in ("first", "repeat"):
        with TerminalProcess(session, "ug", release, f"native-release-{phase}") as terminal:
            terminal.finish()
        assert tomllib.loads(private.read_text()) == {
            "notice": {"hide_rate_limit_model_nudge": True}
        }
        assert "features" not in tomllib.loads(requirements.read_text())
        actual = tomllib.loads(managed.read_text())
        for key in native:
            assert key not in actual
        released_override = session.run(
            "-c", "features.fast_mode=true", "features", "list", binary="codex", timeout=30
        )
        feature_rows = [
            line.split()
            for line in released_override.stdout.splitlines()
            if line.startswith("fast_mode ")
        ]
        assert feature_rows == [["fast_mode", "stable", "true"]], released_override.stdout
