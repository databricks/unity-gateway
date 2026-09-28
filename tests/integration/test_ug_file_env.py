"""Installed-product journeys for launch-only file environment overrides."""

import json
import shlex
import sys
import time
import tomllib
import uuid

import pytest
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL
from utils.evidence import FileTask

INVALID_ENV = [
    pytest.param([], id="not-an-object"),
    pytest.param({"FILE_ENV_CANARY": True}, id="not-a-string"),
    pytest.param({"NOT-PORTABLE": "value"}, id="invalid-name"),
    pytest.param({"FILE_ENV_CANARY": "nul\0value"}, id="nul-value"),
    pytest.param({"File_Env_Canary": "a", "FILE_ENV_CANARY": "b"}, id="case-collision"),
    pytest.param({"OAUTH_TOKEN": "not-a-token"}, id="reserved-auth"),
    pytest.param({"HOME": "/not-the-session-home"}, id="reserved-home"),
    pytest.param({"CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": "0"}, id="reserved-token-fd"),
]

OBSERVED_KEYS = (
    "FILE_ENV_CANARY",
    "ARCA_ISAAC_SESSION_SOURCE",
    "MCP_TOOL_TIMEOUT",
    "FILE_ENV_EMPTY",
    "FILE_ENV_PHASE",
    "FILE_ENV_NATIVE_POLICY",
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_TRACES_EXPORTER",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_BLRP_SCHEDULE_DELAY",
)
OBSERVER = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    f"values = {{name: os.environ.get(name) for name in {OBSERVED_KEYS!r}}}\n"
    "if len(sys.argv) == 2:\n"
    "    Path(sys.argv[1]).write_text(json.dumps(values), encoding='utf-8')\n"
    "else:\n"
    "    print('FILE_ENV_OBSERVATION=' + json.dumps(values))\n"
)


def _remaining(deadline: float) -> int:
    seconds = int(deadline - time.monotonic())
    assert seconds > 0, "File environment lifecycle exceeded five minutes"
    return min(seconds, 120)


def _assert_launch_value_not_saved(session, value: str, agent: str) -> None:
    """Inspect public on-disk output, without creating or changing application state."""
    state = session.home / ".ucode/state.json"
    agent_config = session.home / (
        ".claude/ucode-settings.json" if agent == "claude" else ".codex/ucode.config.toml"
    )
    for path in (state, agent_config):
        text = path.read_text()
        document = json.loads(text) if path.suffix == ".json" else tomllib.loads(text)
        serialized = json.dumps(document)
        assert '"custom_env"' not in serialized, path
        assert json.dumps(value) not in serialized, path


@pytest.mark.installation
@pytest.mark.parametrize("custom_env", INVALID_ENV)
def test_ug_claude_rejects_invalid_file_env_before_launch(session, custom_env):
    """Scenario: pass malformed or reserved custom_env to installed ug claude.

    Expected: validation fails before setup or native --version can run, with
    empty stdout, an actionable custom_env error, and no files in the fresh home.
    No workspace or inference is involved.
    """
    source = session.cwd / "invalid claude environment.json"
    source.write_text(
        json.dumps(
            {
                "spec_version": 1,
                "enabled_agents": [
                    {"agent": "CODING_AGENT_CLAUDE_CODE", "config": {"custom_env": custom_env}}
                ],
            }
        )
    )
    assert not list(session.home.iterdir())

    result = session.run(
        "claude", "-f", str(source), "--", "--version", ok=False, timeout=30, strip_ansi=False
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "custom_env" in result.stderr and "Invalid --config-file" in result.stderr
    assert "Starting" not in result.stderr and "Traceback" not in result.stderr
    assert not list(session.home.iterdir())


@pytest.mark.installation
@pytest.mark.parametrize("custom_env", INVALID_ENV)
def test_ug_codex_rejects_invalid_file_env_before_launch(session, custom_env):
    """Scenario: pass malformed or reserved custom_env to installed ug codex.

    Expected: validation fails before setup or native --version can run, with
    empty stdout, an actionable custom_env error, and no files in the fresh home.
    No workspace or inference is involved.
    """
    source = session.cwd / "invalid codex environment.json"
    source.write_text(
        json.dumps(
            {
                "spec_version": 1,
                "enabled_agents": [
                    {"agent": "CODING_AGENT_CODEX", "config": {"custom_env": custom_env}}
                ],
            }
        )
    )
    assert not list(session.home.iterdir())

    result = session.run(
        "codex", "-f", str(source), "--", "--version", ok=False, timeout=30, strip_ansi=False
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "custom_env" in result.stderr and "Invalid --config-file" in result.stderr
    assert "Starting" not in result.stderr and "Traceback" not in result.stderr
    assert not list(session.home.iterdir())


@pytest.mark.live
@pytest.mark.claude
def test_ug_claude_file_env_add_change_remove_and_omit(live_session, unmanaged_workspace):
    """Scenario: edit a model-less header/env file and relaunch Claude within five minutes.

    Expected: SessionStart records exact add/change values, then native values
    after removal and omission of -f; every launch completes a real file task.
    Foreign telemetry/helper/header leaves in the pre-existing private file and
    native user permissions, model, and hook survive; launch values are not saved
    in ug state/settings. This does not claim OTLP export or machine-wide policy.
    """
    session = live_session
    observer = session.cwd / "observe environment.py"
    observer.write_text(OBSERVER)
    observation = session.cwd / "claude environment.json"
    native_path = session.home / ".claude/settings.json"
    native_path.parent.mkdir(parents=True)
    native_helper = native_path.parent / "native-otel-headers.py"
    native_helper.write_text('print(\'{"x-integration-native": "preserved"}\')\n')
    native_env = {
        "FILE_ENV_CANARY": "native-claude",
        "ARCA_ISAAC_SESSION_SOURCE": "native-session",
        "MCP_TOOL_TIMEOUT": "333333",
        "FILE_ENV_EMPTY": "native-nonempty",
        "FILE_ENV_NATIVE_POLICY": "native-policy",
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_TRACES_EXPORTER": "console",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
    }
    native = {
        "model": CLAUDE_TEST_MODEL,
        "permissions": {"allow": ["Read"], "deny": ["Bash(rm:*)"]},
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": shlex.join(
                                [sys.executable, str(observer), str(observation)]
                            ),
                        }
                    ]
                }
            ]
        },
    }
    # Foreign native leaves predate ug ownership; no gateway config or ug state is seeded.
    private_path = session.home / ".claude/ucode-settings.json"
    private_native = {
        "env": {**native_env, "ANTHROPIC_CUSTOM_HEADERS": "X-Native-Preservation: preserved"},
        "otelHeadersHelper": shlex.join([sys.executable, str(native_helper)]),
        "spinnerTipsEnabled": False,
    }
    private_path.write_text(json.dumps(private_native))
    native_path.write_text(json.dumps(native))
    helper_before = native_helper.read_bytes()
    session.env.update(
        {
            "FILE_ENV_CANARY": "inherited",
            "ARCA_ISAAC_SESSION_SOURCE": "inherited-session",
            "MCP_TOOL_TIMEOUT": "111111",
            "FILE_ENV_EMPTY": "inherited-nonempty",
        }
    )
    session.env.pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        unmanaged_workspace,
        "--disable-databricks-ai-tools",
    )
    configured = json.loads(private_path.read_text())
    assert configured["otelHeadersHelper"] == private_native["otelHeadersHelper"]
    assert all(configured["env"].get(name) == value for name, value in native_env.items())
    assert "X-Native-Preservation: preserved" in configured["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert configured["spinnerTipsEnabled"] is False

    source = session.cwd / "claude launch environment.json"
    marker = uuid.uuid4().hex
    added = {
        "FILE_ENV_CANARY": f'added {marker} "quoted"\nUnicode: λ',
        "ARCA_ISAAC_SESSION_SOURCE": "isaac_cli",
        "MCP_TOOL_TIMEOUT": "444444",
        "FILE_ENV_EMPTY": "",
    }
    changed = {**added, "FILE_ENV_CANARY": f"changed {marker}", "MCP_TOOL_TIMEOUT": "555555"}
    started = time.monotonic()
    deadline = started + 300
    for phase, overrides, use_file in (
        ("add", added, True),
        ("change", changed, True),
        ("remove", None, True),
        ("without-f", changed, False),
    ):
        config = {"http_headers": {"X-File-Env-Journey": marker}}
        if overrides is not None:
            config["custom_env"] = overrides
        assert "models" not in config and "default_models" not in config
        source.write_text(
            json.dumps(
                {
                    "spec_version": 1,
                    "enabled_agents": [{"agent": "CODING_AGENT_CLAUDE_CODE", "config": config}],
                }
            )
        )
        session.env["FILE_ENV_PHASE"] = phase
        task = FileTask(session)
        result = session.run(
            "claude",
            *(["-f", str(source)] if use_file else []),
            "--",
            "-p",
            task.prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
            timeout=_remaining(deadline),
        )
        task.assert_headless_answer("claude", result)
        observed = json.loads(observation.read_text())
        expected = {name: native_env.get(name) for name in OBSERVED_KEYS}
        expected["FILE_ENV_PHASE"] = phase
        # Native Claude strips OTEL_* from hook children; persistence is checked below.
        expected["OTEL_TRACES_EXPORTER"] = None
        if use_file and overrides:
            expected.update(overrides)
        assert observed == expected
        assert json.loads(native_path.read_text()) == native
        assert native_helper.read_bytes() == helper_before
        generated = json.loads(private_path.read_text())
        assert not {"availableModels", "enforceAvailableModels", "modelPicker"} & generated.keys()
        assert "ANTHROPIC_MODEL" not in generated["env"]
        assert generated["otelHeadersHelper"] == private_native["otelHeadersHelper"]
        assert all(generated["env"].get(name) == value for name, value in native_env.items())
        assert "X-Native-Preservation: preserved" in generated["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        assert generated["spinnerTipsEnabled"] is False
        if use_file:
            assert f"X-File-Env-Journey: {marker}" in generated["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        _assert_launch_value_not_saved(session, added["FILE_ENV_CANARY"], "claude")
        _assert_launch_value_not_saved(session, changed["FILE_ENV_CANARY"], "claude")
        session.record(
            f"claude-file-env-{phase}.json", {"observed": observed, "expected": expected}
        )
        assert time.monotonic() < deadline
    assert session.env["FILE_ENV_CANARY"] == "inherited"
    session.assert_not_routed()
    session.record("claude-file-env-timing.json", {"elapsed_seconds": time.monotonic() - started})


@pytest.mark.live
@pytest.mark.codex
def test_ug_codex_file_env_add_change_remove_and_omit(live_session, unmanaged_workspace):
    """Scenario: edit a model-less header/env file and relaunch Codex within five minutes.

    Expected: real sandbox children observe exact add/change values, then inherited
    values after removal and omission of -f; each phase also completes a gateway
    file task. Native telemetry, shell policy, permissions, and model preferences
    survive. Only isolated user settings are authored; no OTLP export is claimed.
    """
    session = live_session
    observer = session.cwd / "observe environment.py"
    observer.write_text(OBSERVER)
    native_path = session.home / ".codex/config.toml"
    native_path.parent.mkdir(parents=True)
    # Ordinary native preferences and shell policy, not a generated gateway profile.
    native_path.write_text(
        f'model = "{CODEX_TEST_MODEL}"\n'
        'model_reasoning_effort = "low"\n'
        'approval_policy = "on-request"\n'
        'sandbox_mode = "read-only"\n'
        '[shell_environment_policy.set]\nFILE_ENV_NATIVE_POLICY = "native-policy"\n'
        '[otel]\nenvironment = "native-file-env"\nexporter = "none"\n'
        'trace_exporter = "none"\nlog_user_prompt = false\n'
        "[notice]\nhide_rate_limit_model_nudge = true\n"
    )
    native = tomllib.loads(native_path.read_text())
    inherited = {
        "FILE_ENV_CANARY": "inherited",
        "ARCA_ISAAC_SESSION_SOURCE": "inherited-session",
        "MCP_TOOL_TIMEOUT": "111111",
        "FILE_ENV_EMPTY": "inherited-nonempty",
        "FILE_ENV_NATIVE_POLICY": "parent-policy",
        "OTEL_RESOURCE_ATTRIBUTES": "integration_scope=inherited",
        "OTEL_METRIC_EXPORT_INTERVAL": "60000",
        "OTEL_BLRP_SCHEDULE_DELAY": "5000",
    }
    session.env.update(inherited)
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        unmanaged_workspace,
        "--disable-databricks-ai-tools",
    )

    source = session.cwd / "codex launch environment.json"
    marker = uuid.uuid4().hex
    added = {
        "FILE_ENV_CANARY": f'added {marker} "quoted"\nUnicode: λ',
        "ARCA_ISAAC_SESSION_SOURCE": "isaac_cli",
        "MCP_TOOL_TIMEOUT": "444444",
        "FILE_ENV_EMPTY": "",
        "OTEL_RESOURCE_ATTRIBUTES": f"integration_scope=added,integration_marker={marker}",
        "OTEL_METRIC_EXPORT_INTERVAL": "1000",
        "OTEL_BLRP_SCHEDULE_DELAY": "2000",
    }
    changed = {
        **added,
        "FILE_ENV_CANARY": f"changed {marker}",
        "MCP_TOOL_TIMEOUT": "555555",
        "OTEL_RESOURCE_ATTRIBUTES": f"integration_scope=changed,integration_marker={marker}",
        "OTEL_METRIC_EXPORT_INTERVAL": "750",
        "OTEL_BLRP_SCHEDULE_DELAY": "150",
    }
    started = time.monotonic()
    deadline = started + 300
    for phase, overrides, use_file in (
        ("add", added, True),
        ("change", changed, True),
        ("remove", None, True),
        ("without-f", changed, False),
    ):
        config = {"http_headers": {"X-File-Env-Journey": marker}}
        if overrides is not None:
            config["custom_env"] = overrides
        assert "models" not in config and "default_models" not in config
        source.write_text(
            json.dumps(
                {
                    "spec_version": 1,
                    "enabled_agents": [{"agent": "CODING_AGENT_CODEX", "config": config}],
                }
            )
        )
        session.env["FILE_ENV_PHASE"] = phase
        observation = session.run(
            "codex",
            *(["-f", str(source)] if use_file else []),
            "--",
            "sandbox",
            "--",
            sys.executable,
            str(observer),
            timeout=_remaining(deadline),
        )
        records = [
            line.removeprefix("FILE_ENV_OBSERVATION=")
            for line in observation.stdout.splitlines()
            if line.startswith("FILE_ENV_OBSERVATION=")
        ]
        assert len(records) == 1, observation.stdout
        observed = json.loads(records[0])
        expected = {name: inherited.get(name) for name in OBSERVED_KEYS}
        expected.update(FILE_ENV_PHASE=phase, FILE_ENV_NATIVE_POLICY="native-policy")
        if use_file and overrides:
            expected.update(overrides)
        assert observed == expected

        task = FileTask(session)
        result = session.run(
            "codex",
            *(["-f", str(source)] if use_file else []),
            "--",
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--model",
            CODEX_TEST_MODEL,
            task.prompt,
            timeout=_remaining(deadline),
        )
        task.assert_headless_answer("codex", result)
        current = tomllib.loads(native_path.read_text())
        assert all(current.get(key) == value for key, value in native.items())
        generated = tomllib.loads((session.home / ".codex/ucode.config.toml").read_text())
        assert not {"model", "model_reasoning_effort", "model_catalog_json"} & generated.keys()
        assert "otel" not in generated and "shell_environment_policy" not in generated
        if use_file:
            headers = generated["model_providers"]["Databricks"]["http_headers"]
            assert headers["X-File-Env-Journey"] == marker
        _assert_launch_value_not_saved(session, added["FILE_ENV_CANARY"], "codex")
        _assert_launch_value_not_saved(session, changed["FILE_ENV_CANARY"], "codex")
        session.record(f"codex-file-env-{phase}.json", {"observed": observed, "expected": expected})
        assert time.monotonic() < deadline
    assert all(session.env[name] == value for name, value in inherited.items())
    session.assert_not_routed()
    session.record("codex-file-env-timing.json", {"elapsed_seconds": time.monotonic() - started})
