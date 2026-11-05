"""CUJ8: launcher sessions where a second program also writes Claude's OS-managed settings.

A launcher that wraps `ug claude` can also, on every launch, rewrite Claude's root-owned managed
settings from its own template. This CUJ reproduces that workflow behaviorally: the same
launch argv and environment, the same managed-settings field set, and the same drift, preserve,
and sudo write steps. Proprietary template content (hook scripts, announcements, internal
endpoints) is replaced with neutral stand-ins; OTEL points at this CUJ's workspace.
"""

import copy
import json
import os
import re
import shutil
from pathlib import Path

import pytest
from databricks.sdk.errors import DatabricksError, NotFound

from tests.integration.utils.evidence import fresh_session_id
from tests.integration.utils.managed import MANAGED_CONFIGS_PATH, assert_no_managed_config
from tests.integration.utils.terminal import TerminalProcess

from .base import BaseCujTest, bearer
from .helpers.constants import CLAUDE, MANAGED_PATHS
from .helpers.evidence import SessionEvidence, assert_inference_evidence, claude_file_task
from .helpers.session import (
    MACHINE_WIDE_LEAK,
    MANAGED_STATE_FILES,
    UserSession,
    dirty_runner_message,
    record_machine_wide_leak,
)
from .helpers.terminal import Terminal
from .helpers.tui_request_recorder import TuiRequestRecorder

CUJ_NAME = "CUJ 8 · Second managed-settings writer"

pytestmark = [pytest.mark.live, pytest.mark.workspace_isolated]

MANAGED_SETTINGS = MANAGED_PATHS[0]
SESSIONS = 3

# The second writer's own model choices. Haiku deliberately differs from what discovery would pick.
WRITER_DEFAULTS = {
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-5-5[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "system.ai.claude-sonnet-5-5[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "system.ai.claude-haiku-4-5",
}
# The task selects the writer's Haiku default through `ANTHROPIC_MODEL`, since a `--model` pin
# would turn smart routing off. Claude 2.1.290 sends `output_config.effort`
# for Opus 5.5, which the gateway rejects before Claude's native retry, so Opus is avoided.
TASK_MODEL = "system.ai.claude-haiku-4-5"
FAMILY_DEFAULT_KEYS = tuple(
    f"ANTHROPIC_DEFAULT_{family}_MODEL" for family in ("FABLE", "OPUS", "SONNET", "HAIKU")
)

# Keys the writer leaves to ug, and the header lines it owns (a ug-routed launcher's behavior).
WRITER_DEFERRED_TOP_LEVEL = ("apiKeyHelper",)
WRITER_DEFERRED_ENV = ("CLAUDE_CODE_API_KEY_HELPER_TTL_MS",)
WRITER_HEADER_NAMES = frozenset(
    {"x-databricks-use-coding-agent-mode", "databricks-ai-gateway-request-tags"}
)
WRITER_HEADERS = (
    "x-databricks-use-coding-agent-mode: true\n"
    'Databricks-Ai-Gateway-Request-Tags: {"source": "ug-cuj8-second-writer", "org": "engineering"}'
)
# Fields ug owns in the writer's file, so they may differ from the writer's template. Every other
# field the writer sets must survive ug's launches verbatim.
UG_OWNED_FIELDS = frozenset(
    {"apiKeyHelper", "env.ANTHROPIC_CUSTOM_HEADERS", "env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS"}
)
# ug prints `! ...` warnings and `ERROR ...` errors, possibly inside a boxed panel.
UG_WARNING_OR_ERROR = re.compile(r"(?m)^[\s│╭╰]*(?:!|ERROR)\s")
# ug prints one of these before each privileged write, even when sudo needs no password.
UG_WRITE_NOTICE = re.compile(r"Enter password (?:once )?to configure")
ORCHESTRATOR_CONTEXT = "Smart Router Orchestrator is on for this session."
# Stand-in for each of the writer's hook scripts.
WRITER_HOOK = {"type": "command", "command": "true", "timeout": 10}


def writer_template(base_url: str, workspace_url: str, otel_headers_helper: Path) -> dict:
    """The second writer's managed settings: a launcher's full field set with neutral or CUJ values."""
    otel = f"{workspace_url}/api/2.0/otel"
    return {
        "permissions": {
            "disableBypassPermissionsMode": "disable",
            "deny": ["WebSearch", "Artifact", "ArtifactComments", "ArtifactData"],
            "allow": [
                "WebFetch(domain:code.claude.com)",
                "Bash(ls:*)",
                "Bash(pwd:*)",
                "Bash(echo:*)",
                "Bash(git status:*)",
                "Read",
            ],
            "sandbox": {"enabled": True, "allowUnsandboxedCommands": True},
            "allowManagedPermissionRulesOnly": True,
        },
        "spinnerVerbs": {"mode": "replace", "verbs": ["Testing"]},
        "attribution": {"commit": "", "pr": ""},
        "channelsEnabled": True,
        "allowedChannelPlugins": [{"marketplace": "ug-cuj8-marketplace", "plugin": "ug-cuj8"}],
        "prStatusFooterEnabled": False,
        "idleCompaction": False,
        "env": {
            "ENABLE_TOOL_SEARCH": "true",
            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "true",
            "MCP_TOOL_TIMEOUT": "420000",
            "CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0",
            "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "0",
            "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
            **{
                f"DISABLE_PROMPT_CACHING{suffix}": "0"
                for suffix in ("", "_FABLE", "_HAIKU", "_OPUS", "_SONNET")
            },
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
            "OTEL_LOG_USER_PROMPTS": "1",
            "OTEL_LOG_TOOL_DETAILS": "1",
            "OTEL_LOG_RAW_API_BODIES": "1",
            "OTEL_LOG_TOOL_CONTENT": "1",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_METRIC_EXPORT_INTERVAL": "60000",
            "OTEL_LOGS_EXPORT_INTERVAL": "5000",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": otel,
            **{
                f"OTEL_EXPORTER_OTLP_{signal}_{field}": value
                for signal, path in (("METRICS", "metrics"), ("LOGS", "logs"), ("TRACES", "traces"))
                for field, value in (
                    ("PROTOCOL", "http/protobuf"),
                    ("ENDPOINT", f"{otel}/v1/{path}"),
                    ("HEADERS", "content-type=application/x-protobuf"),
                )
            },
            "OTEL_EXPORTER_OTLP_HEADERS": "content-type=application/x-protobuf",
            "CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH": "524288",
            "OTEL_RESOURCE_ATTRIBUTES": "deployment=ug-cuj8",
            "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS": "43200000",
            "ANTHROPIC_BASE_URL": f"{base_url}/ai-gateway/anthropic",
            "ANTHROPIC_CUSTOM_HEADERS": WRITER_HEADERS,
            **WRITER_DEFAULTS,
            "CLAUDE_CODE_USE_GATEWAY": "1",
            "ENABLE_PROMPT_CACHING_1H": "1",
            "LOOP_GUARDRAIL_ENABLED": "1",
            "LAUNCHER_GIT_STATUSLINE_ENABLED": "1",
            "DISABLE_AUTOUPDATER": "1",
        },
        "companyAnnouncements": ["ug CUJ 8 second managed-settings writer."],
        "hooks": {
            event: [{**({"matcher": matcher} if matcher else {}), "hooks": [WRITER_HOOK]}]
            for event, matcher in (
                ("WorktreeCreate", None),
                ("UserPromptSubmit", None),
                ("PreToolUse", "Bash"),
                ("PostToolUse", "Task"),
                ("SessionStart", None),
                ("StopFailure", None),
            )
        },
        "otelHeadersHelper": str(otel_headers_helper),
        "modelPicker": {
            "options": [
                {"model": WRITER_DEFAULTS["ANTHROPIC_DEFAULT_OPUS_MODEL"], "label": "Opus"},
                {"model": WRITER_DEFAULTS["ANTHROPIC_DEFAULT_SONNET_MODEL"], "label": "Sonnet"},
                {"model": WRITER_DEFAULTS["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "label": "Haiku"},
                {"model": "system.ai.glm-5-3", "label": "GLM 5.3", "behavesAs": "claude-haiku-4-5"},
                {"model": "system.ai.kimi-k3", "label": "Kimi K3", "behavesAs": "claude-haiku-4-5"},
                {"model": "system.ai.claude-haiku-5-5", "label": "Haiku 5.5"},
            ],
            "replaceBuiltInOptions": True,
        },
        "disableWorkflows": True,
        "workflowKeywordTriggerEnabled": False,
    }


def launch_env(base_url: str) -> dict[str, str]:
    """The environment the launcher passes to `ug claude`, minus custom OAuth and router names."""
    return {
        "LAUNCHER_SESSION_SOURCE": "launcher_cli",
        "RTK_TELEMETRY_DISABLED": "1",
        "LAUNCHER_AUTOPILOT_ALLOWED": "true",
        "ANTHROPIC_BASE_URL": f"{base_url}/ai-gateway/anthropic",
        "ANTHROPIC_CUSTOM_HEADERS": WRITER_HEADERS,
        **WRITER_DEFAULTS,
        "CLAUDE_CODE_USE_GATEWAY": "1",
        "ENABLE_PROMPT_CACHING_1H": "1",
        "ANTHROPIC_MODEL": "haiku",
        "SMART_ROUTER_CONFIG_VERSION": "subagent_orch_v0_claude_only",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "1",
        "UG_AUTO_UPGRADE_AGENT": "1",
    }


def claude_args() -> list[str]:
    """The Claude args the launcher forwards after `--`, with a neutral review nudge."""
    return [
        "--settings",
        json.dumps({"deniedMcpServers": [{"serverName": "web-search"}]}),
        "--session-id",
        fresh_session_id(),
        "--append-system-prompt",
        "When you finish a change, suggest reviewing it before pushing.",
    ]


def _header_lines(headers: object) -> tuple[list[str], list[str]]:
    """Split header lines into the writer's own and everyone else's, keeping order."""
    owned, other = [], []
    for line in headers.splitlines() if isinstance(headers, str) else []:
        name, separator, _ = line.partition(":")
        (owned if separator and name.strip().lower() in WRITER_HEADER_NAMES else other).append(line)
    return owned, other


def _writer_view(settings: dict) -> dict:
    """What the writer compares: everything except the keys it defers and foreign header lines."""
    view = copy.deepcopy(settings)
    for key in WRITER_DEFERRED_TOP_LEVEL:
        view.pop(key, None)
    env = view.get("env")
    if isinstance(env, dict):
        for key in WRITER_DEFERRED_ENV:
            env.pop(key, None)
        owned, _ = _header_lines(env.get("ANTHROPIC_CUSTOM_HEADERS"))
        env["ANTHROPIC_CUSTOM_HEADERS"] = owned
    return view


def _merged_for_write(existing: dict, template: dict) -> dict:
    """The template, keeping the deferred keys and foreign header lines already in the file."""
    merged = copy.deepcopy(template)
    for key in WRITER_DEFERRED_TOP_LEVEL:
        if key in existing:
            merged[key] = existing[key]
    existing_env = existing.get("env") if isinstance(existing.get("env"), dict) else {}
    for key in WRITER_DEFERRED_ENV:
        if key in existing_env:
            merged["env"][key] = existing_env[key]
    owned, _ = _header_lines(merged["env"].get("ANTHROPIC_CUSTOM_HEADERS"))
    _, foreign = _header_lines(existing_env.get("ANTHROPIC_CUSTOM_HEADERS"))
    merged["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join([*owned, *foreign])
    return merged


def _fields(settings: dict) -> dict:
    """Top-level keys, with `env` flattened to `env.<KEY>`."""
    fields = {key: value for key, value in settings.items() if key != "env"}
    fields.update({f"env.{key}": value for key, value in settings.get("env", {}).items()})
    return fields


def read_managed_settings() -> dict | None:
    return json.loads(MANAGED_SETTINGS.read_text()) if MANAGED_SETTINGS.exists() else None


class TestSecondManagedSettingsWriter(BaseCujTest):
    WORKSPACE_URL = "https://dbc-6a91dc96-859b.cloud.databricks.com"

    @pytest.fixture(autouse=True)
    def unmanaged_workspace(self, setup_workspace):
        self._assert_unmanaged()
        yield
        self._assert_unmanaged()

    def _assert_unmanaged(self):
        # The launcher's workspace publishes no CodingAgentConfig, so neither may this one.
        try:
            payload = self.workspace.api_client.do("GET", MANAGED_CONFIGS_PATH)
        except NotFound:
            return
        except DatabricksError as error:
            raise RuntimeError(f"Workspace API failed: {type(error).__name__}") from None
        assert_no_managed_config(payload)
        assert not payload.get("next_page_token"), "Incomplete CodingAgentConfig listing"

    @pytest.fixture
    def live_session(self, unmanaged_workspace, tmp_path, request):
        assert os.name == "posix", "CUJ8 requires a disposable POSIX runner"
        assert not any(path.exists() for path in MANAGED_PATHS), dirty_runner_message(
            request.config.stash.get(MACHINE_WIDE_LEAK, None)
        )
        binary = shutil.which("ug")
        assert binary, "Install ug before running CUJ8"
        for tool in (CLAUDE, "databricks", "sudo"):
            assert shutil.which(tool), f"Install the required CLI before running CUJ8: {tool}"
        session = UserSession(
            tmp_path, Path(binary), tmp_path / "artifacts", bearer(self.workspace)
        )
        directory_existed = MANAGED_SETTINGS.parent.exists()
        try:
            yield session
        finally:
            try:
                # Revert restores the second writer's file; then remove it like an uninstall.
                if any((session.home / ".ucode" / name).is_file() for name in MANAGED_STATE_FILES):
                    with TerminalProcess(
                        session, "ug", [str(session.binary), "revert"], "cuj8-teardown-revert"
                    ) as terminal:
                        terminal.finish()
            finally:
                session.run("rm", "-f", str(MANAGED_SETTINGS), binary="sudo")
                if not directory_existed and MANAGED_SETTINGS.parent.exists():
                    session.run("rmdir", str(MANAGED_SETTINGS.parent), binary="sudo")
                record_machine_wide_leak(request)

    @pytest.fixture
    def request_recorder(self, live_session):
        with TuiRequestRecorder(self.workspace.config.host) as recorder:
            yield recorder

    def _second_writer_update(self, session: UserSession, template: dict) -> bool:
        """The launcher's per-launch step: write only on drift, through `sudo mkdir`/`chattr`/`cp`/`chmod`."""
        existing = read_managed_settings()
        if existing is not None and _writer_view(existing) == _writer_view(template):
            return False
        staged = session.home / "second-writer-managed-settings.json"
        staged.write_text(json.dumps(_merged_for_write(existing or {}, template), indent=2))
        session.run("mkdir", "-p", str(MANAGED_SETTINGS.parent), binary="sudo")
        if existing is not None:
            session.run("chattr", "-i", str(MANAGED_SETTINGS), binary="sudo", ok=False)
        session.run("cp", str(staged), str(MANAGED_SETTINGS), binary="sudo")
        session.run("chmod", "a+rx", str(MANAGED_SETTINGS.parent), binary="sudo")
        session.run("chmod", "a+r", str(MANAGED_SETTINGS), binary="sudo")
        return True

    @pytest.mark.claude
    @pytest.mark.tui
    def test_launcher_sessions_prompt_at_most_once(self, live_session, request_recorder):
        """Scenario: run three launcher sessions. Each first applies the second writer's
        managed settings (the launcher's field set, its own modelPicker and opus/sonnet/haiku defaults,
        written only on drift), then launches `ug claude --workspace <ws> -- <launcher's Claude
        args>` in a real TUI with the launcher's launch environment, including its smart-routing
        preset, and completes a file task.

        Expected: the first session makes at most two privileged writes (the writer's initial
        file and one ug write, counted from ug's per-write password notices); later sessions make
        none. ug prints no warnings or errors, and `/model` shows the writer's picker.
        Every field the writer set survives unchanged except the ones ug owns (`apiKeyHelper`,
        its TTL, and ug's own header lines), so neither settings file gains a discovered default
        such as fable. The orchestrator stays on, each task's recorded gateway inference is
        served on the writer's Haiku default, and `ug revert` restores the writer's file exactly.

        Deviations from a real launcher: CI sudo is passwordless, so prompts are counted as privileged
        writes of the root-owned file. Auth uses the CUJ bearer instead of a custom OAuth
        app, and ug's default router recipe replaces a server-side one. `ANTHROPIC_MODEL`
        selects Haiku because the gateway rejects Claude's Opus 5.5 `output_config.effort`.
        """
        session = live_session
        recorder = request_recorder
        # Like a launcher's helper, authenticate OTEL export with the developer's own token.
        otel_headers_helper = session.home / ".config/ug-cuj8/get_otel_headers.sh"
        otel_headers_helper.parent.mkdir(parents=True)
        otel_headers_helper.write_text(
            '#!/bin/sh\nprintf \'{"Authorization": "Bearer %s"}\' "$DATABRICKS_BEARER"\n'
        )
        otel_headers_helper.chmod(0o700)
        template = writer_template(recorder.url, self.WORKSPACE_URL, otel_headers_helper)
        inherited_env = dict(session.env)
        session.env.update(launch_env(recorder.url))
        writes_per_session = []
        try:
            for number in range(1, SESSIONS + 1):
                writer_wrote = self._second_writer_update(session, template)
                before = MANAGED_SETTINGS.read_text()
                if number == 1:
                    writer_file = json.loads(before)
                task = claude_file_task(session)
                task.prompt += " Do not delegate."
                evidence = SessionEvidence(session.home, CLAUDE)
                checkpoint = recorder.checkpoint()
                args = [CLAUDE, "--workspace", recorder.url, "--", *claude_args()]
                with Terminal(session, f"cuj8-session-{number}", args) as tui:
                    tui.boot()
                    picker = tui.open_model_picker(
                        model_visible=lambda screen: all(
                            option["label"] in screen
                            for option in template["modelPicker"]["options"]
                        )
                    )
                    assert "fable" not in picker.lower(), picker
                    tui.submit(task.prompt)
                    tui.task(evidence, task)
                    tui.exit_normally()
                output = session.redact("".join(tui.output), strip_ansi=True)
                task.assert_completed(session, CLAUDE)
                assert_inference_evidence(recorder, checkpoint, CLAUDE, task, TASK_MODEL)
                # The launcher's routing preset stays live: the orchestrator reaches the task request.
                assert any(
                    ORCHESTRATOR_CONTEXT.encode() in request.body
                    for request in recorder.requests_after(checkpoint)
                    if request.method == "POST" and task.prompt.encode() in request.body
                ), "Smart Router Orchestrator context missing from the task request"
                assert not UG_WARNING_OR_ERROR.search(output), output
                ug_writes = max(
                    len(UG_WRITE_NOTICE.findall(output)),
                    int(MANAGED_SETTINGS.read_text() != before),
                )
                assert ug_writes <= (1 if number == 1 else 0), output
                writes_per_session.append(int(writer_wrote) + ug_writes)
        finally:
            # The teardown `ug revert` must not inherit the launch-only environment.
            session.env.clear()
            session.env.update(inherited_env)

        assert writes_per_session[0] <= 2 and writes_per_session[1:] == [0] * (SESSIONS - 1), (
            writes_per_session
        )
        managed = read_managed_settings()
        managed_fields = _fields(managed)
        diverged = {
            field: managed_fields.get(field, "<missing>")
            for field, value in _fields(template).items()
            if field not in UG_OWNED_FIELDS and managed_fields.get(field) != value
        }
        assert not diverged, diverged
        owned_headers, _ = _header_lines(managed["env"]["ANTHROPIC_CUSTOM_HEADERS"])
        assert owned_headers == WRITER_HEADERS.splitlines(), owned_headers
        private = json.loads((session.home / ".claude/ucode-settings.json").read_text())
        for settings in (managed, private):
            assert {key: settings["env"].get(key) for key in FAMILY_DEFAULT_KEYS} == {
                "ANTHROPIC_DEFAULT_FABLE_MODEL": None,
                **WRITER_DEFAULTS,
            }, settings

        # Like a user leaving ug, revert hands the writer back exactly the file it wrote.
        with TerminalProcess(
            session, "ug", [str(session.binary), "revert"], "cuj8-revert"
        ) as revert:
            revert.finish()
        assert read_managed_settings() == writer_file
