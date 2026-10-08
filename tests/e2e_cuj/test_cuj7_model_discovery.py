"""CUJ7: model discovery and scoped inference in a dedicated unmanaged workspace."""

import json
import os
import shutil
import tomllib
from pathlib import Path

import pytest
from databricks.sdk.errors import DatabricksError, NotFound

from tests.integration.utils.evidence import FileTask
from tests.integration.utils.managed import MANAGED_CONFIGS_PATH, assert_no_managed_config
from tests.integration.utils.model_discovery import (
    assert_claude_system_models_in_picker,
    assert_codex_default_models,
)
from tests.integration.utils.terminal import TerminalProcess

from .base import BaseCujTest, bearer
from .helpers.constants import (
    CLAUDE,
    CLAUDE_HAIKU_MODEL_SERVICE,
    CLAUDE_SONNET_MODEL_SERVICE,
    CODEX,
    CODEX_LUNA_MODEL_SERVICE,
    MANAGED_PATHS,
    MODEL_SERVICE_SCHEMA,
)
from .helpers.evidence import (
    SessionEvidence,
    assert_claude_headless_model,
    assert_inference_evidence,
    claude_file_task,
)
from .helpers.session import (
    MACHINE_WIDE_LEAK,
    MANAGED_STATE_FILES,
    UserSession,
    dirty_runner_message,
    record_machine_wide_leak,
)
from .helpers.terminal import Terminal
from .helpers.tui_request_recorder import TuiRequestRecorder

CUJ_NAME = "CUJ 7 · Unmanaged model discovery"

pytestmark = [pytest.mark.live, pytest.mark.workspace_isolated]

CLAUDE_FAMILY_DEFAULTS = {
    f"ANTHROPIC_DEFAULT_{family}_MODEL": (
        CLAUDE_SONNET_MODEL_SERVICE if family == "SONNET" else CLAUDE_HAIKU_MODEL_SERVICE
    )
    for family in ("FABLE", "OPUS", "SONNET", "HAIKU")
}
# Inert admin-owned Codex settings outside ug's owned keys: a sibling provider and a display key.
CODEX_ADMIN_CONFIG = """file_opener = "none"

[model_providers.AdminProvider]
name = "Admin provider"
base_url = "https://admin-provider.invalid/v1"
wire_api = "responses"
"""
MANAGED_INPUT = {
    CLAUDE: (MANAGED_PATHS[0], json.dumps({"env": CLAUDE_FAMILY_DEFAULTS})),
    CODEX: (MANAGED_PATHS[1], CODEX_ADMIN_CONFIG),
}
# Only an interactive launch rewrites the OS-managed file; ug's gateway base URL proves it did.
MANAGED_BASE_URL = {
    CLAUDE: (("env", "ANTHROPIC_BASE_URL"), "/ai-gateway/anthropic"),
    CODEX: (("model_providers", "Databricks", "base_url"), "/ai-gateway/codex/v1"),
}


def _selected_agent(request):
    agents = [agent for agent in (CLAUDE, CODEX) if request.node.get_closest_marker(agent)]
    assert len(agents) == 1, "CUJ7 cases must select exactly one agent"
    return agents[0]


def _parse_managed(agent, text):
    return json.loads(text) if agent == CLAUDE else tomllib.loads(text)


def _read_managed(session, agent):
    path = MANAGED_INPUT[agent][0]
    return _parse_managed(agent, session.run(str(path), binary="cat", timeout=30).stdout)


def _assert_contains(document, seeded, path=()):
    """Every seeded leaf survives unchanged at its original path."""
    for key, value in seeded.items():
        location = ".".join((*path, key))
        assert isinstance(document, dict) and key in document, f"Missing seeded {location}"
        if isinstance(value, dict):
            _assert_contains(document[key], value, (*path, key))
        else:
            assert document[key] == value, {
                "path": location,
                "seeded": value,
                "observed": document[key],
            }


def _assert_rewritten(document, agent, recorder):
    keys, suffix = MANAGED_BASE_URL[agent]
    value = document
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    assert value == f"{recorder.url}{suffix}", value


def _assert_revert_restores(session, agent, seeded, name):
    with TerminalProcess(session, "ug", [str(session.binary), "revert"], name) as terminal:
        terminal.finish()
    assert _read_managed(session, agent) == seeded


class TestUnmanagedModelDiscovery(BaseCujTest):
    WORKSPACE_URL = "https://dbc-14e376e8-6541.cloud.databricks.com"

    @pytest.fixture(autouse=True)
    def unmanaged_workspace(self, setup_workspace):
        self._assert_unmanaged()
        yield
        self._assert_unmanaged()

    def _assert_unmanaged(self):
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
        """Keep each launch unconfigured; the class-scoped cuj fixture requires a published config."""
        assert os.name == "posix", "CUJ7 requires a disposable POSIX runner"
        assert not any(path.exists() for path in MANAGED_PATHS), dirty_runner_message(
            request.config.stash.get(MACHINE_WIDE_LEAK, None)
        )
        binary = shutil.which("ug")
        assert binary, "Install ug before running CUJ7"
        for tool in (_selected_agent(request), "databricks"):
            assert shutil.which(tool), f"Install the required CLI before running CUJ7: {tool}"
        session = UserSession(
            tmp_path,
            Path(binary),
            tmp_path / "artifacts",
            bearer(self.workspace),
        )
        try:
            yield session
        finally:
            try:
                session.revert_machine_wide(
                    "cleanup-revert", "CUJ7 teardown left machine-wide agent settings"
                )
            finally:
                record_machine_wide_leak(request)

    @pytest.fixture
    def request_recorder(self, live_session):
        with TuiRequestRecorder(self.workspace.config.host) as recorder:
            yield recorder

    @pytest.fixture
    def managed_input(self, live_session, request):
        """Seed the selected agent's OS-managed input; stop recording before guarded revert."""
        session = live_session
        agent = _selected_agent(request)
        path, text = MANAGED_INPUT[agent]
        managed_path = str(path)
        managed_directory = path.parent
        directory_existed = managed_directory.exists()
        try:
            if not directory_existed:
                session.run("install", "-d", "-m", "0755", str(managed_directory), binary="sudo")
            session.run("tee", managed_path, binary="sudo", input_text=text)
            with TuiRequestRecorder(self.workspace.config.host) as recorder:
                yield _parse_managed(agent, text), recorder
        finally:
            try:
                # Revert restores our seeded input; revert_machine_wide would reject that file.
                if any((session.home / ".ucode" / name).is_file() for name in MANAGED_STATE_FILES):
                    with TerminalProcess(
                        session, "ug", [str(session.binary), "revert"], "family-defaults-revert"
                    ) as terminal:
                        terminal.finish()
            finally:
                session.run("rm", "-f", managed_path, binary="sudo")
                if not directory_existed and managed_directory.exists():
                    session.run("rmdir", str(managed_directory), binary="sudo")

    @pytest.mark.claude
    @pytest.mark.tui
    def test_case_07_configured_claude_discovers_system_models(self, live_session):
        """Scenario: configure Claude, then launch without source overrides or discovery flags.

        Expected: native discovery caches system.ai models as raw IDs or recognized Claude
        gateway aliases and shows a discovered picker entry.
        """
        session = live_session
        session.configure(
            [
                "configure",
                "--agents",
                CLAUDE,
                "--workspace",
                self.workspace.config.host,
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ]
        )
        with Terminal(session, "case-07-system-models", [CLAUDE]) as tui:
            tui.boot()
            screen = tui.open_model_picker(
                model_visible=lambda _text: session.claude_gateway_cache_ready()
            )
            tui.exit_normally()
        assert_claude_system_models_in_picker(session, screen)

    @pytest.mark.codex
    def test_case_08_configured_codex_uses_default_models(self, live_session):
        """Scenario: configure Codex, then launch without source overrides.

        Expected: unmanaged configuration leaves model selection to Codex's native default;
        ug records system.ai discovery while model and reasoning preferences remain unset;
        app-server exposes native GPT entries without a generated provider/parent-scoped catalog.
        """
        session = live_session
        session.configure(
            [
                "configure",
                "--agents",
                CODEX,
                "--workspace",
                self.workspace.config.host,
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ]
        )
        models = session.codex_model_ids(["app-server", "--listen", "stdio://"])
        assert_codex_default_models(session, models)

    @pytest.mark.claude
    @pytest.mark.parametrize("model_owner", ["ug", "claude"])
    def test_ug_claude_headless_fresh_model_location(
        self, live_session, request_recorder, model_owner
    ):
        """Scenario: launch fresh scoped Claude with --model before/after ug's separator.

        Expected: Haiku completes a file task without prior configuration or routing.
        """
        session = live_session
        task = claude_file_task(session)
        model = CLAUDE_HAIKU_MODEL_SERVICE
        model_args = ["--model", model]
        evidence = SessionEvidence(session.home, CLAUDE)

        result = session.run(
            CLAUDE,
            "--workspace",
            request_recorder.url,
            "--model-location",
            MODEL_SERVICE_SCHEMA,
            *(model_args if model_owner == "ug" else []),
            "--",
            *(model_args if model_owner == "claude" else []),
            "-p",
            task.prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
            timeout=240,
        )
        task.assert_headless_answer(CLAUDE, result)
        assert evidence.completed(task), "No completed native turn matched the file task"
        assert_claude_headless_model(result, model)
        assert_inference_evidence(
            request_recorder, 0, CLAUDE, task, model, parent_schema=MODEL_SERVICE_SCHEMA
        )
        session.assert_not_routed()

    @pytest.mark.claude
    @pytest.mark.parametrize("model_owner", ["ug", "claude"])
    def test_ug_claude_preserves_preexisting_managed_family_defaults(
        self, live_session, managed_input, model_owner
    ):
        """Scenario: select Sonnet before/after ug's separator over OS-managed defaults.

        Expected: ug preserves every family default, and the selected Sonnet family
        completes a file task through the preconfigured Sonnet service, not a discovered default.
        """
        session = live_session
        task = claude_file_task(session)
        model = CLAUDE_SONNET_MODEL_SERVICE
        model_args = ["--model", "sonnet"]
        seeded, recorder = managed_input
        evidence = SessionEvidence(session.home, CLAUDE)
        result = session.run(
            CLAUDE,
            "--workspace",
            recorder.url,
            *(model_args if model_owner == "ug" else []),
            "--",
            *(model_args if model_owner == "claude" else []),
            "-p",
            task.prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
            timeout=240,
        )
        task.assert_headless_answer(CLAUDE, result)
        assert evidence.completed(task), "No completed native turn matched the file task"
        assert_claude_headless_model(result, model)
        assert_inference_evidence(recorder, 0, CLAUDE, task, model)
        _assert_contains(_read_managed(session, CLAUDE), seeded)
        session.assert_not_routed()

    @pytest.mark.claude
    @pytest.mark.tui
    def test_ug_claude_tui_rewrites_managed_settings_preserving_family_defaults(
        self, live_session, managed_input
    ):
        """Scenario: select Sonnet in an interactive launch over OS-managed defaults.

        Expected: unlike headless launches, the TTY lets ug rewrite the OS-managed file; the
        rewrite keeps every family default, the TUI task reaches the preconfigured Sonnet service,
        and revert restores the seeded file.
        """
        session = live_session
        task = claude_file_task(session)
        model = CLAUDE_SONNET_MODEL_SERVICE
        seeded, recorder = managed_input
        evidence = SessionEvidence(session.home, CLAUDE)
        with Terminal(
            session,
            "family-defaults-tui",
            [CLAUDE, "--workspace", recorder.url, "--model", "sonnet"],
        ) as tui:
            tui.boot()
            tui.submit(task.prompt)
            tui.task(evidence, task)
            tui.exit_normally()
        task.assert_completed(session, CLAUDE)
        assert_inference_evidence(recorder, 0, CLAUDE, task, model)
        settings = _read_managed(session, CLAUDE)
        _assert_rewritten(settings, CLAUDE, recorder)
        _assert_contains(settings, seeded)
        session.assert_not_routed()
        _assert_revert_restores(session, CLAUDE, seeded, "family-defaults-tui-revert")

    @pytest.mark.codex
    @pytest.mark.parametrize("model_position", ["before_separator", "exec"])
    def test_ug_codex_headless_fresh_model_location(
        self, live_session, request_recorder, model_position
    ):
        """Scenario: launch fresh scoped Codex with --model before ug's separator or in exec.

        Expected: GPT Luna completes a file task without prior configuration or routing.
        """
        session = live_session
        task = FileTask(session)
        model = CODEX_LUNA_MODEL_SERVICE
        model_args = ["--model", model]
        evidence = SessionEvidence(session.home, CODEX)

        result = session.run(
            CODEX,
            "--workspace",
            request_recorder.url,
            "--model-location",
            MODEL_SERVICE_SCHEMA,
            *(model_args if model_position == "before_separator" else []),
            "--",
            "exec",
            "--skip-git-repo-check",
            "--json",
            *(model_args if model_position == "exec" else []),
            task.prompt,
            timeout=240,
        )
        task.assert_headless_answer(CODEX, result)
        turn = evidence.completed(task)
        assert turn and set(turn.models) == {model}, turn
        assert_inference_evidence(
            request_recorder, 0, CODEX, task, model, parent_schema=MODEL_SERVICE_SCHEMA
        )
        session.assert_not_routed()

    @pytest.mark.codex
    @pytest.mark.parametrize("model_position", ["before_separator", "exec"])
    def test_ug_codex_preserves_preexisting_managed_config(
        self, live_session, managed_input, model_position
    ):
        """Scenario: select GPT Luna before ug's separator or in exec over OS-managed admin config.

        Expected: ug preserves every admin setting, and the scoped Luna service completes a
        file task without routing.
        """
        session = live_session
        task = FileTask(session)
        model = CODEX_LUNA_MODEL_SERVICE
        model_args = ["--model", model]
        seeded, recorder = managed_input
        evidence = SessionEvidence(session.home, CODEX)
        result = session.run(
            CODEX,
            "--workspace",
            recorder.url,
            "--model-location",
            MODEL_SERVICE_SCHEMA,
            *(model_args if model_position == "before_separator" else []),
            "--",
            "exec",
            "--skip-git-repo-check",
            "--json",
            *(model_args if model_position == "exec" else []),
            task.prompt,
            timeout=240,
        )
        task.assert_headless_answer(CODEX, result)
        turn = evidence.completed(task)
        assert turn and set(turn.models) == {model}, turn
        assert_inference_evidence(
            recorder, 0, CODEX, task, model, parent_schema=MODEL_SERVICE_SCHEMA
        )
        _assert_contains(_read_managed(session, CODEX), seeded)
        session.assert_not_routed()

    @pytest.mark.codex
    @pytest.mark.tui
    def test_ug_codex_tui_rewrites_managed_config_preserving_admin_settings(
        self, live_session, managed_input
    ):
        """Scenario: select scoped GPT Luna in an interactive launch over OS-managed admin config.

        Expected: unlike headless launches, the TTY lets ug rewrite the OS-managed file; the
        rewrite keeps every admin setting, the TUI task reaches the scoped Luna service, and
        revert restores the seeded file.
        """
        session = live_session
        task = FileTask(session)
        model = CODEX_LUNA_MODEL_SERVICE
        seeded, recorder = managed_input
        evidence = SessionEvidence(session.home, CODEX)
        with Terminal(
            session,
            "managed-config-tui",
            [
                CODEX,
                "--workspace",
                recorder.url,
                "--model-location",
                MODEL_SERVICE_SCHEMA,
                "--model",
                model,
            ],
        ) as tui:
            tui.boot()
            tui.submit(task.prompt)
            tui.task(evidence, task)
            tui.exit_normally()
        turn = evidence.completed(task)
        assert turn and set(turn.models) == {model}, turn
        assert_inference_evidence(
            recorder, 0, CODEX, task, model, parent_schema=MODEL_SERVICE_SCHEMA
        )
        config = _read_managed(session, CODEX)
        _assert_rewritten(config, CODEX, recorder)
        _assert_contains(config, seeded)
        session.assert_not_routed()
        _assert_revert_restores(session, CODEX, seeded, "managed-config-tui-revert")
