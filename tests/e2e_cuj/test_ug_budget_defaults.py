"""Live model-selection check for a fixed workspace budget recommendation."""

import re
import tomllib

import pytest
from base import BaseCujTest
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.cuj, pytest.mark.tui]


class TestCujBudgetDefaults(BaseCujTest):
    WORKSPACE_URL = "https://dbc-497adeef-62c0.cloud.databricks.com"
    BUDGET_ID = "25c4ce5c-fcd6-4d00-8266-69030cc1a236"
    SOL_MODEL = "system.ai.gpt-5-6-sol"
    LUNA_MODEL = "system.ai.gpt-5-6-luna"

    def test_bare_ug_uses_luna_budget_recommendation(self, live_session):
        """Scenario: launch bare ``ug`` with the fixed 2% Luna budget tier active.

        Expected: the real backend recommends Codex/Luna and the Codex TUI selects
        Luna over its managed Sol default, without budget writes or an inference task.
        """
        session = live_session
        config_path = "/api/ai-gateway/v2/coding-agent-configs"
        payload = self.workspace.api_client.do("GET", path=config_path)
        config = payload["coding_agent_configs"][0]
        assert config["default_agent"] == "CODING_AGENT_CLAUDE_CODE", config
        assert config["smart_defaults"]["budget_id"] == self.BUDGET_ID, config
        assert config["smart_defaults"]["tiers"] == [
            {
                "spending_percentage": 0.02,
                "recommended_agent": "CODING_AGENT_CODEX",
                "recommended_model": self.LUNA_MODEL,
            }
        ], config

        session.run(
            "configure",
            "--workspace",
            self.WORKSPACE_URL,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )
        model_config = session.home / ".codex" / "ucode.config.toml"
        assert tomllib.loads(model_config.read_text())["model"] == self.SOL_MODEL

        recommendation = self.workspace.api_client.do(
            "POST", path=config_path + ":recommendModel", body={}
        )
        assert recommendation.get("recommended_agent") == "CODING_AGENT_CODEX", recommendation
        assert recommendation.get("recommended_model") == self.LUNA_MODEL, recommendation

        with AgentTerminal(session, "codex", [str(session.binary)], "budget-luna") as tui:
            tui.boot(timeout=240)
            assert tomllib.loads(model_config.read_text())["model"] == self.LUNA_MODEL
            tui.wait_for(
                lambda text: re.search(rf"model:\s+{re.escape(self.LUNA_MODEL)}\s", text),
                "Codex's selected model to be Luna",
            )
            tui.exit_normally()
