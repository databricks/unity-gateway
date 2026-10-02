"""One cross-agent CUJ, one dedicated workspace, six fresh interactive sessions."""

from base import BaseCujTest
from helpers.constants import CodingAgent
from helpers.evidence import FileTask, SessionEvidence
from helpers.terminal import Terminal


class TestCujSmartRouting(BaseCujTest):
    WORKSPACE_URL = "https://dbc-1a9622fc-2e91.cloud.databricks.com/"

    def test_cuj_smart_routing(self, cuj):
        """Scenario: route both agents' real first prompts, override, then disable routing.

        Expected: each successful live decision is tied to the sole submitted
        prompt and the completed native turn's model; a banner is insufficient.
        Explicit supported non-default models bypass routing. Republish BOTH
        flags false, reconfigure the same home, and require ordinary defaults
        without fresh routing activity. Each final answer includes an unpredictable
        file value absent from its prompt, and each TUI exits normally.

        No fixed winner/different-picks assertion. Claude uses response metadata;
        Codex uses completed-turn model context (client execution evidence, not
        independent gateway-side verification). Fixture teardown restores the
        original workspace policy even when an assertion or process fails.
        """
        session, workspace = cuj
        assert workspace.url == self.WORKSPACE_URL.rstrip("/")
        published = workspace.original
        assert published["spec_version"] == 1
        assert published["default_agent"] == CodingAgent.CLAUDE_CODE
        entries = published["enabled_agents"]
        assert len(entries) == 2
        configs = {entry["agent"]: entry["config"] for entry in entries}
        assert set(configs) == {CodingAgent.CLAUDE_CODE, CodingAgent.CODEX}
        agents = {
            "claude": configs[CodingAgent.CLAUDE_CODE],
            "codex": configs[CodingAgent.CODEX],
        }
        supported, defaults, overrides = {}, {}, {}
        for agent, config in agents.items():
            assert config["smart_routing"]["enabled"] is True
            assert config.get("tracing", {}).get("enabled", False) is False
            assert not config.get("smart_defaults") and not config.get("spend_tiers")
            assert set(config["models"]) == {"model_services"}, (
                "No MPS or schema source in this CUJ"
            )
            offered = config["models"]["model_services"]
            assert len(set(offered)) >= 2 and all(
                model.startswith("system.ai.") for model in offered
            )
            supported[agent] = workspace.model_ids(agent)
            assert set(offered) <= supported[agent], (
                "Published targets are absent from the live catalog"
            )
            defaults[agent] = config["default_models"]["default_model"]
            assert defaults[agent] in offered
            overrides[agent] = next(model for model in offered if model != defaults[agent])
        session.record(
            "scenario",
            {
                "workspace": self.WORKSPACE_URL,
                "defaults": defaults,
                "overrides": overrides,
                "live_supported": {a: sorted(m) for a, m in supported.items()},
            },
        )

        # Configure once through the public CLI. Every launch is a fresh native
        # session, but reuse the home to catch stale settings after reconfigure.
        session.command(
            "configure-enabled",
            ["configure", "--workspace", self.WORKSPACE_URL, "--disable-databricks-ai-tools"],
        )
        session_ids = set()
        for agent in ("claude", "codex"):
            workspace.assert_unchanged()
            task = FileTask.create(session.project)
            evidence = SessionEvidence(session.home, agent)
            with Terminal(session, f"{agent}-routed", [agent], evidence=evidence) as tui:
                tui.boot(agent)
                tui.submit(task.prompt)
                tui.task(evidence, task)
                tui.exit_normally()
            result = evidence.assert_applied(task, supported[agent], routed=True)
            session.record(f"{agent}-routed-evidence", result)
            assert (agent, result["session_id"]) not in session_ids
            session_ids.add((agent, result["session_id"]))

            # Explicit non-default model: new session, no router activity.
            workspace.assert_unchanged()
            task = FileTask.create(session.project)
            evidence = SessionEvidence(session.home, agent)
            with Terminal(
                session,
                f"{agent}-explicit",
                [agent, "--model", overrides[agent]],
                evidence=evidence,
            ) as tui:
                tui.boot(agent)
                tui.submit(task.prompt)
                tui.task(evidence, task)
                tui.exit_normally()
            result = evidence.assert_applied(
                task, supported[agent], routed=False, expected=overrides[agent]
            )
            session.record(f"{agent}-explicit-evidence", result)
            assert (agent, result["session_id"]) not in session_ids
            session_ids.add((agent, result["session_id"]))

        # Publish BOTH flags together, verify visibility, and reconfigure this home.
        workspace.disable_routing()
        assert all(
            entry["config"]["smart_routing"]["enabled"] is False
            for entry in workspace.config()["enabled_agents"]
        )
        session.command(
            "configure-disabled",
            ["configure", "--workspace", self.WORKSPACE_URL, "--disable-databricks-ai-tools"],
        )
        for agent in ("claude", "codex"):
            workspace.assert_unchanged()
            task = FileTask.create(session.project)
            evidence = SessionEvidence(session.home, agent)
            with Terminal(session, f"{agent}-disabled", [agent], evidence=evidence) as tui:
                tui.boot(agent)
                tui.submit(task.prompt)
                tui.task(evidence, task)
                tui.exit_normally()
            result = evidence.assert_applied(
                task, supported[agent], routed=False, expected=defaults[agent]
            )
            session.record(f"{agent}-disabled-evidence", result)
            assert (agent, result["session_id"]) not in session_ids
            session_ids.add((agent, result["session_id"]))
        workspace.assert_unchanged()
        assert len(session_ids) == 6
