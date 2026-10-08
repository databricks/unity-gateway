"""CUJ7: model discovery and scoped inference in a dedicated unmanaged workspace."""

import json
import os
import shutil
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
    INFERENCE_PATHS,
    MANAGED_PATHS,
    MODEL_SERVICE_SCHEMA,
)
from .helpers.evidence import SessionEvidence, assert_served, canonical_model, claude_file_task
from .helpers.session import UserSession
from .helpers.terminal import Terminal
from .helpers.tui_request_recorder import TuiRequestRecorder

CUJ_NAME = "CUJ 7 · Unmanaged model discovery"

pytestmark = [pytest.mark.live, pytest.mark.cuj7, pytest.mark.workspace_isolated]


def _assert_claude_service_task(recorder, task, turn, model):
    assert turn, "No completed native turn matched the file task"
    requests = [
        request
        for request in recorder.requests_after(0)
        if request.method == "POST"
        and request.path == INFERENCE_PATHS[CLAUDE]
        and task.prompt in json.dumps(request.payload)
    ]
    assert requests, "No Claude inference request matched the file task"
    for request in requests:
        assert_served(recorder, request, model)


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

    @pytest.fixture
    def live_session(self, unmanaged_workspace, tmp_path):
        """Keep each launch unconfigured; the class-scoped cuj fixture requires a published config."""
        assert os.name == "posix", "CUJ7 requires a disposable POSIX runner"
        assert not any(path.exists() for path in MANAGED_PATHS), (
            "Existing machine-wide agent settings; use a clean disposable runner"
        )
        binary = shutil.which("ug")
        assert binary, "Install ug before running CUJ7"
        for tool in (CLAUDE, CODEX, "databricks"):
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
            session.revert_machine_wide(
                "cleanup-revert", "CUJ7 teardown left machine-wide agent settings"
            )

    @pytest.fixture
    def request_recorder(self, live_session):
        with TuiRequestRecorder(self.workspace.config.host) as recorder:
            yield recorder

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
        turn = evidence.completed(task)
        _assert_claude_service_task(request_recorder, task, turn, model)
        session.assert_not_routed()

    @pytest.mark.claude
    @pytest.mark.parametrize("model_owner", ["ug", "claude"])
    def test_ug_claude_preserves_preexisting_managed_family_defaults(
        self, live_session, request_recorder, model_owner
    ):
        """Scenario: select Sonnet before/after ug's separator over OS-managed defaults.

        Expected: ug preserves every family default, and the selected Sonnet family
        completes a file task through the preconfigured Sonnet service, not a discovered default.
        """
        session = live_session
        task = claude_file_task(session)
        model = CLAUDE_SONNET_MODEL_SERVICE
        model_args = ["--model", "sonnet"]
        defaults = {
            f"ANTHROPIC_DEFAULT_{family}_MODEL": (
                model if family == "SONNET" else CLAUDE_HAIKU_MODEL_SERVICE
            )
            for family in ("FABLE", "OPUS", "SONNET", "HAIKU")
        }
        managed_path = "/etc/claude-code/managed-settings.json"
        evidence = SessionEvidence(session.home, CLAUDE)
        session.run("install", "-d", "-m", "0755", "/etc/claude-code", binary="sudo")
        try:
            session.run(
                "tee", managed_path, binary="sudo", input_text=json.dumps({"env": defaults})
            )
            result = session.run(
                CLAUDE,
                "--workspace",
                request_recorder.url,
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
            turn = evidence.completed(task)
            _assert_claude_service_task(request_recorder, task, turn, model)
            settings = json.loads(session.run(managed_path, binary="cat", timeout=30).stdout)
            assert {key: settings.get("env", {}).get(key) for key in defaults} == defaults
            session.assert_not_routed()
        finally:
            try:
                with TerminalProcess(
                    session, "ug", [str(session.binary), "revert"], "family-defaults-revert"
                ) as terminal:
                    terminal.finish()
            finally:
                session.run("rm", "-f", managed_path, binary="sudo")

    @pytest.mark.codex
    @pytest.mark.parametrize("model_position", ["before_separator", "exec"])
    def test_ug_codex_headless_fresh_model_location(self, live_session, model_position):
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
            self.workspace.config.host,
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
        assert turn and set(map(canonical_model, turn.models)) == {canonical_model(model)}, turn
        session.assert_not_routed()
