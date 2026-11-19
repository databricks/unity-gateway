"""Tests for Copilot smart-routing of ``task`` subagents."""

from __future__ import annotations

import json
import shlex

import pytest
from typer.testing import CliRunner

from ucode import cli as cli_mod
from ucode.smart_routing import copilot_routing
from ucode.smart_routing.routing import RoutingDecision

WS = "https://example.databricks.com"
SMALL_REF = "databricks/fixture-small"
LARGE_REF = "databricks-responses/fixture-large"
REFS = {"system.ai.fixture-small": SMALL_REF, "system.ai.fixture-large": LARGE_REF}
PAYLOAD = {
    "sessionId": "session-1",
    "toolCallId": "call-1",
    "toolName": "task",
    "toolArgs": {
        "name": "reverse",
        "prompt": "Write a Python one-liner that reverses a string.",
        "agent_type": "prober",
        "mode": "sync",
    },
}


def _decision(model: str) -> RoutingDecision:
    return RoutingDecision(model=model, rationale="cheap and fast", router_name="stable")


@pytest.fixture(autouse=True)
def decisions_path(tmp_path, monkeypatch):
    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(copilot_routing, "DECISIONS_PATH", path)
    return path


def test_task_call_is_rewritten_to_the_routed_registry_model(decisions_path):
    prompts = []

    def decide(task):
        prompts.append(task)
        return _decision("system.ai.fixture-large"), None

    output = copilot_routing.route_pre_tool_use(
        PAYLOAD, workspace=WS, token="tok", refs=REFS, decision_fn=decide
    )

    assert prompts == [PAYLOAD["toolArgs"]["prompt"]]
    assert output == {"modifiedArgs": {**PAYLOAD["toolArgs"], "model": LARGE_REF}}
    record = json.loads(decisions_path.read_text())
    assert (record["router_model"], record["requested_model"]) == (
        "system.ai.fixture-large",
        LARGE_REF,
    )
    assert (record["session_id"], record["tool_use_id"]) == ("session-1", "call-1")


def test_other_tools_and_failed_routes_are_left_alone(decisions_path):
    def fail(task):
        return None, "router returned HTTP 500"

    other = {**PAYLOAD, "toolName": "bash"}
    for payload, decide in ((other, None), (PAYLOAD, fail)):
        assert (
            copilot_routing.route_pre_tool_use(
                payload, workspace=WS, token="tok", refs=REFS, decision_fn=decide
            )
            is None
        )
    assert not decisions_path.exists()


def test_router_is_offered_claude_harness_options_for_the_registry_models(monkeypatch):
    seen = {}

    def select(workspace, token, task, options, **kwargs):
        seen.update(options=list(options), router_name=kwargs["router_name"])
        return _decision("system.ai.fixture-small"), None

    monkeypatch.setattr(copilot_routing.routing, "select_route", select)
    monkeypatch.delenv("SMART_ROUTER_NAME", raising=False)

    decision, error = copilot_routing.request_routing_decision(WS, "tok", "task", REFS)

    assert error is None and decision.model == "system.ai.fixture-small"
    assert seen["options"] == [
        ("system.ai.fixture-small", "claude"),
        ("system.ai.fixture-large", "claude"),
    ]


def test_hook_argv_offers_at_most_the_routers_model_limit():
    refs = {f"system.ai.fixture-{n}": f"databricks/fixture-{n}" for n in range(30)}
    argv = copilot_routing.hook_argv({"workspace": WS, "profile": "p"}, refs)

    offered = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--model"]
    assert offered == [
        f"{model}={ref}" for model, ref in list(refs.items())[: copilot_routing.MAX_ROUTE_OPTIONS]
    ]
    assert argv[1:3] == [copilot_routing.ROUTING_HOOK_COMMAND_MARKER, "route-subagent"]
    assert argv[argv.index("--profile") + 1] == "p"


def test_plugin_hook_runs_only_for_task_calls(tmp_path):
    argv = ["/bin/ug", "copilot-router-hook", "route-subagent", "--host", WS]

    copilot_routing.write_plugin(tmp_path / "plugin", argv)

    manifest = json.loads((tmp_path / "plugin/.claude-plugin/plugin.json").read_text())
    hooks = json.loads((tmp_path / "plugin" / manifest["hooks"]).read_text())
    (hook,) = hooks["hooks"]["preToolUse"]
    assert hook["matcher"] == "task"
    assert shlex.split(hook["bash"]) == argv
    assert hook["timeoutSec"] > 30


def test_orchestration_uses_a_command_session_start_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_SMART_ROUTER_ORCHESTRATOR", "1")
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    directory = tmp_path / "plugin"
    copilot_routing.write_plugin(directory, ["/bin/ug", "copilot-router-hook", "route-subagent"])
    hooks = json.loads((directory / "hooks/hooks.json").read_text())["hooks"]
    (hook,) = hooks["sessionStart"]
    assert hook["type"] == "command"
    assert shlex.split(hook["bash"])[1:] == [
        "-m",
        "ucode.smart_routing.orchestrator",
        "--agent",
        "copilot",
    ]
    monkeypatch.setenv("ENABLE_SMART_ROUTER_ORCHESTRATOR", "0")
    directory = tmp_path / "disabled"
    copilot_routing.write_plugin(directory, ["/bin/ug", "copilot-router-hook", "route-subagent"])
    assert "sessionStart" not in json.loads((directory / "hooks/hooks.json").read_text())["hooks"]


def test_orchestration_context_uses_native_tasks_and_respects_off_and_child(monkeypatch):
    from ucode.smart_routing import orchestrator

    monkeypatch.setattr(orchestrator, "enabled", lambda **_kwargs: True)
    context = orchestrator.hook_output({"sessionId": "parent"}, agent="copilot")
    assert "native `task` tool" in context["additionalContext"]
    assert "do not delegate again" in context["additionalContext"]
    assert orchestrator.hook_output({"agentId": "child"}, agent="copilot") is None
    monkeypatch.setattr(orchestrator, "enabled", lambda **_kwargs: False)
    assert orchestrator.hook_output({"sessionId": "parent"}, agent="copilot") is None


class TestRouterHookCommand:
    ARGS = [
        "copilot-router-hook",
        "route-subagent",
        "--host",
        WS,
        "--model",
        "system.ai.fixture-large=" + LARGE_REF,
    ]
    runner = CliRunner()

    @pytest.fixture(autouse=True)
    def enabled(self, monkeypatch):
        monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
        monkeypatch.delenv("UCODE_SESSION_ENV_FILE", raising=False)
        monkeypatch.setattr(cli_mod, "_router_hook_token", lambda *_args: "tok")

    def invoke(self, payload):
        return self.runner.invoke(cli_mod.app, self.ARGS, input=json.dumps(payload))

    def test_prints_the_rewritten_args(self, monkeypatch):
        monkeypatch.setattr(
            copilot_routing,
            "request_routing_decision",
            lambda *_args, **_kwargs: (_decision("system.ai.fixture-large"), None),
        )

        result = self.invoke(PAYLOAD)

        assert result.exit_code == 0
        assert json.loads(result.stdout)["modifiedArgs"]["model"] == LARGE_REF

    def test_disabled_routing_prints_nothing(self, monkeypatch):
        monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "0")

        result = self.invoke(PAYLOAD)

        assert (result.exit_code, result.stdout) == (0, "")

    def test_malformed_registry_mapping_leaves_the_call_unchanged(self, monkeypatch):
        result = self.runner.invoke(
            cli_mod.app,
            [*self.ARGS[:-1], "invalid-mapping"],
            input=json.dumps(PAYLOAD),
        )
        assert (result.exit_code, result.stdout) == (0, "")

    def test_a_failure_exits_zero_because_copilot_denies_the_call_on_nonzero(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError("network down")

        monkeypatch.setattr(copilot_routing, "request_routing_decision", boom)

        result = self.invoke(PAYLOAD)

        assert (result.exit_code, result.stdout) == (0, "")
