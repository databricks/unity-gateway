"""Live budget recommendation and default-model checks for a fixed workspace tier."""

import json
import re
from decimal import ROUND_HALF_UP, Decimal

from tests.integration.utils.terminal import AgentTerminal

from .base import BaseCujTest
from .helpers.constants import CLAUDE, CODEX, CodingAgent
from .helpers.workspace import Workspace

CUJ_NAME = "CUJ 5 · Budget-driven defaults"


class _BudgetDefaultsBase(BaseCujTest):
    WORKSPACE_URL = "https://dbc-497adeef-62c0.cloud.databricks.com"
    BUDGET_ID = "25c4ce5c-fcd6-4d00-8266-69030cc1a236"
    SONNET_MODEL = "system.ai.claude-sonnet-4-6"
    SOL_MODEL = "system.ai.gpt-5-6-sol"
    LUNA_MODEL = "system.ai.gpt-5-6-luna"


class TestCujBudgetDefaultsBelowTier(_BudgetDefaultsBase):
    CLIENT_ID_ENV = "UG_BUDGET_CUJ_SP_CLIENT_ID"
    CLIENT_SECRET_ENV = "UG_BUDGET_CUJ_SP_CLIENT_SECRET"

    def test_bare_ug_uses_claude_default_below_budget_tier(self, cuj):
        """Scenario: launch bare ``ug`` as a fresh principal below the fixed 1% tier.

        Expected: the published config keeps Claude/Sonnet as the default, the real recommendation
        also selects Claude/Sonnet, and the real Claude TUI starts on that model without a prompt,
        inference request, budget write, or account-login flow. A fresh principal may have no
        spend counters yet; when the backend returns an effective threshold, omitted spend is
        treated as zero and must remain below the 1% tier. If both figures are absent, this case
        verifies default selection without a numeric spend assertion.
        """
        session, workspace, _ = cuj
        config_path = "/api/ai-gateway/v2/coding-agent-configs"
        config = workspace.config()
        assert config["default_agent"] == CodingAgent.CLAUDE_CODE, config
        claude = Workspace.agent_configs(config)[CodingAgent.CLAUDE_CODE]
        claude_defaults = claude["default_models"]
        assert claude_defaults["default_model"] == self.SONNET_MODEL, config
        assert config["smart_defaults"]["budget_id"] == self.BUDGET_ID, config
        assert config["smart_defaults"]["tiers"] == [
            {
                "spending_percentage": 0.01,
                "recommended_agent": CodingAgent.CODEX,
                "recommended_model": self.LUNA_MODEL,
            }
        ], config

        session.configure(
            [
                "configure",
                "--workspace",
                self.WORKSPACE_URL,
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ]
        )

        recommendation = workspace.client.api_client.do(
            "POST", path=config_path + ":recommendModel", body={}
        )
        assert recommendation.get("recommended_agent") == CodingAgent.CLAUDE_CODE, recommendation
        assert recommendation.get("recommended_model") == self.SONNET_MODEL, recommendation

        threshold = recommendation.get("effective_threshold")
        spend = recommendation.get("current_spend")
        if threshold is not None:
            threshold = Decimal(str(threshold))
            assert threshold > 0, recommendation
            spend = Decimal("0") if spend is None else Decimal(str(spend))
            assert spend >= 0, recommendation
            assert spend / threshold < Decimal("0.01"), recommendation
        else:
            # A fresh principal can have no usage counter; that does not establish a numeric ratio.
            assert spend is None, recommendation

        with AgentTerminal(session, CLAUDE, [str(session.binary)], "budget-default-claude") as tui:
            tui.boot(timeout=240)
            claude_settings = session.home / ".claude" / "ucode-settings.json"
            settings = json.loads(claude_settings.read_text())
            assert settings["env"]["ANTHROPIC_MODEL"] == self.SONNET_MODEL, settings
            tui.wait_for(
                lambda text: re.search(r"Claude Sonnet 4\.6\s*·", text),
                "Claude's native Sonnet model header",
            )
            tui.exit_normally()


class TestCujBudgetDefaults(_BudgetDefaultsBase):
    def test_bare_ug_shows_luna_recommendation_and_starts_codex(self, cuj):
        """Scenario: launch bare ``ug`` with the fixed 1% Luna budget tier active.

        Expected: the read-only `usage` command reports the backend's spend and threshold, then
        the real backend recommends Codex/Luna, the launch panel displays that recommendation, and
        the native Codex TUI starts and exits normally, without budget writes or an inference task.
        Applying the recommended Luna model over the managed Sol default is outside this coverage.
        """
        session, workspace, _ = cuj
        config_path = "/api/ai-gateway/v2/coding-agent-configs"
        config = workspace.config()
        assert config["default_agent"] == CodingAgent.CLAUDE_CODE, config
        codex_agent = Workspace.agent_configs(config)[CodingAgent.CODEX]
        live_default_model = codex_agent.get("default_models", {}).get("default_model")
        assert live_default_model == self.SOL_MODEL, (
            f"Live Codex config model mismatch: expected {self.SOL_MODEL!r}, "
            f"got {live_default_model!r}"
        )
        assert config["smart_defaults"]["budget_id"] == self.BUDGET_ID, config
        assert config["smart_defaults"]["tiers"] == [
            {
                "spending_percentage": 0.01,
                "recommended_agent": CodingAgent.CODEX,
                "recommended_model": self.LUNA_MODEL,
            }
        ], config

        session.configure(
            [
                "configure",
                "--workspace",
                self.WORKSPACE_URL,
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ]
        )

        before_usage = workspace.client.api_client.do(
            "POST", path=config_path + ":recommendModel", body={}
        )
        usage = session.run("usage", timeout=120)
        after_usage = workspace.client.api_client.do(
            "POST", path=config_path + ":recommendModel", body={}
        )
        for recommendation in (before_usage, after_usage):
            assert recommendation.get("recommended_agent") == CodingAgent.CODEX, recommendation
            assert recommendation.get("recommended_model") == self.LUNA_MODEL, recommendation

        match = re.search(
            r"Budget spend:\s*\$([\d,]+\.\d{2})\s+of\s+\$([\d,]+\.\d{2})\s+\((\d+)%\)",
            session.redact(usage.stdout + usage.stderr),
        )
        assert match, usage.stdout + usage.stderr
        displayed_spend, displayed_threshold, displayed_percent = match.groups()
        displayed_spend = Decimal(displayed_spend.replace(",", ""))
        displayed_threshold = Decimal(displayed_threshold.replace(",", ""))
        cent = Decimal("0.01")

        before_spend = Decimal(str(before_usage["current_spend"]))
        after_spend = Decimal(str(after_usage["current_spend"]))
        before_threshold = Decimal(str(before_usage["effective_threshold"]))
        after_threshold = Decimal(str(after_usage["effective_threshold"]))
        assert before_spend <= after_spend, (before_usage, after_usage)
        assert before_threshold == after_threshold, (before_usage, after_usage)
        assert displayed_threshold == before_threshold.quantize(cent, rounding=ROUND_HALF_UP)
        assert (
            before_spend.quantize(cent, rounding=ROUND_HALF_UP)
            <= displayed_spend
            <= after_spend.quantize(cent, rounding=ROUND_HALF_UP)
        ), (before_usage, after_usage, displayed_spend)
        assert (
            int(format(float(before_spend / before_threshold), ".0%").removesuffix("%"))
            <= int(displayed_percent)
            <= int(format(float(after_spend / after_threshold), ".0%").removesuffix("%"))
        ), (before_usage, after_usage, usage)

        with AgentTerminal(session, CODEX, [str(session.binary)], "budget-luna") as tui:
            tui.boot(timeout=240)
            transcript = session.redact(
                "".join(tui.output) + "\n" + tui.visible,
                strip_ansi=True,
            )
            assert re.search(
                rf"Recommended agent is Codex with model {re.escape(self.LUNA_MODEL)}\.",
                transcript,
            ), (
                "Codex launch did not display the backend recommendation "
                f"for {self.LUNA_MODEL!r}:\n{transcript}"
            )
            tui.exit_normally()


class TestCujBudgetDefaultsExplicitClaude(_BudgetDefaultsBase):
    def test_ug_claude_starts_on_sonnet_despite_codex_luna_recommendation(self, cuj):
        """Scenario: launch explicit ``ug claude`` for an above-tier principal.

        Expected: the live backend recommends Codex/Luna, and the Claude launch displays
        that recommendation while its generated model setting and native header select Sonnet.
        No task is submitted; this checks startup selection, not the model used for inference.
        """
        session, workspace, _ = cuj
        session.configure(
            [
                "configure",
                "--workspace",
                self.WORKSPACE_URL,
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ]
        )
        recommendation = workspace.client.api_client.do(
            "POST", path="/api/ai-gateway/v2/coding-agent-configs:recommendModel", body={}
        )
        assert recommendation.get("recommended_agent") == CodingAgent.CODEX, recommendation
        assert recommendation.get("recommended_model") == self.LUNA_MODEL, recommendation

        with AgentTerminal(
            session, CLAUDE, [str(session.binary), CLAUDE], "explicit-claude"
        ) as tui:
            tui.boot(timeout=240)
            transcript = session.redact(
                "".join(tui.output) + "\n" + tui.visible,
                strip_ansi=True,
            )
            assert re.search(
                rf"Recommended agent is Codex with model {re.escape(self.LUNA_MODEL)}\.",
                transcript,
            ), (
                "Claude launch did not display the backend recommendation "
                f"for {self.LUNA_MODEL!r}:\n{transcript}"
            )
            claude_settings = session.home / ".claude" / "ucode-settings.json"
            settings = json.loads(claude_settings.read_text())
            assert settings["env"]["ANTHROPIC_MODEL"] == self.SONNET_MODEL, settings
            tui.wait_for(
                lambda text: re.search(r"Claude Sonnet 4\.6\s*·", text),
                "Claude's native Sonnet model header",
            )
            tui.exit_normally()
