"""CUJ7: model discovery and scoped inference in a dedicated unmanaged workspace."""

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

from .base import BaseCujTest
from .helpers.constants import CLAUDE, CODEX, MANAGED_PATHS
from .helpers.evidence import SessionEvidence
from .helpers.session import UserSession
from .helpers.terminal import Terminal

pytestmark = [pytest.mark.live, pytest.mark.cuj7, pytest.mark.workspace_isolated]


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
        """Reuse the session helper; the shared cuj fixture requires a managed config."""
        assert os.name == "posix", "CUJ7 requires a disposable POSIX runner"
        assert not any(path.exists() for path in MANAGED_PATHS), (
            "Existing machine-wide agent settings; use a clean disposable runner"
        )
        binary = shutil.which("ug")
        assert binary, "Install ug before running CUJ7"
        for tool in (CLAUDE, CODEX, "databricks"):
            assert shutil.which(tool), f"Install the required CLI before running CUJ7: {tool}"
        try:
            authorization = self.workspace.config.authenticate().get("Authorization", "")
        except DatabricksError as error:
            raise RuntimeError(f"Workspace authentication failed: {type(error).__name__}") from None
        assert authorization.startswith("Bearer ") and authorization.removeprefix("Bearer "), (
            "Service-principal authentication did not return a bearer token"
        )
        return UserSession(
            tmp_path,
            Path(binary),
            tmp_path / "artifacts",
            authorization.removeprefix("Bearer "),
        )

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
    def test_ug_claude_headless_fresh_model_location(self, live_session):
        """Scenario: launch fresh Claude with --model-location ug_e2e.models.

        Expected: Haiku completes a file task without prior configuration or routing.
        """
        session = live_session
        task = FileTask(session)
        model = "ug_e2e.models.claude_haiku"
        evidence = SessionEvidence(session.home, CLAUDE)

        result = session.run(
            CLAUDE,
            "--workspace",
            self.workspace.config.host,
            "--model-location",
            "ug_e2e.models",
            "--",
            "--model",
            model,
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
        assert turn and set(turn.models) == {model}, turn
        session.assert_not_routed()

    @pytest.mark.codex
    def test_ug_codex_headless_fresh_model_location(self, live_session):
        """Scenario: launch fresh Codex with --model-location ug_e2e.models.

        Expected: GPT Luna completes a file task without prior configuration or routing.
        """
        session = live_session
        task = FileTask(session)
        model = "ug_e2e.models.gpt_luna"
        evidence = SessionEvidence(session.home, CODEX)

        result = session.run(
            CODEX,
            "--workspace",
            self.workspace.config.host,
            "--model-location",
            "ug_e2e.models",
            "--",
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--model",
            model,
            task.prompt,
            timeout=240,
        )
        task.assert_headless_answer(CODEX, result)
        turn = evidence.completed(task)
        assert turn and set(turn.models) == {model}, turn
        session.assert_not_routed()
