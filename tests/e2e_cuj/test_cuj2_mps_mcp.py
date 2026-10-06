"""CUJ2: configure two real agents from per-agent MPS resources with one MCP."""

from __future__ import annotations

import json
import shutil
import tomllib
import uuid

from databricks.sdk.service.catalog import ListModelProviderServicesRequestView

from tests.integration.utils.evidence import (
    agent_sessions,
    assert_no_terminal_api_error,
    assistant_answers,
    is_child_session,
)
from tests.integration.utils.terminal import AgentTerminal

from .base import BaseCujTest
from .helpers.constants import (
    CLAUDE,
    CODEX,
    CODING_AGENT_BY_CLI_NAME,
    MODEL_PROVIDER_SERVICE_FIXTURES,
    SANDBOX_MCP_SERVICE_NAME,
    SANDBOX_MCP_TOOL_IDENTIFIERS,
    TOOL_CALL_EVENT_TYPES,
    TOOL_RESULT_EVENT_TYPES,
    WEB_SEARCH_MCP_SERVICE_NAME,
)
from .helpers.workspace import Workspace

WORKSPACE_URL = "https://dbc-0dcf95cf-e357.cloud.databricks.com"


def _agent_config(agent: str) -> dict:
    mps_name, model = MODEL_PROVIDER_SERVICE_FIXTURES[agent]
    return {
        "agent": CODING_AGENT_BY_CLI_NAME[agent],
        "config": {
            "models": {"model_provider_service": mps_name},
            "default_models": {"default_model": model},
            "smart_routing": {"enabled": False},
            "tracing": {"enabled": False},
        },
    }


def _assert_cuj2_config(config: dict) -> None:
    actual = Workspace.policy(config)
    actual.pop("name", None)
    expected = {
        "spec_version": 1,
        "default_agent": CODING_AGENT_BY_CLI_NAME[CODEX],
        "enabled_agents": [_agent_config(CLAUDE), _agent_config(CODEX)],
        "mcp_servers": {"names": [SANDBOX_MCP_SERVICE_NAME]},
    }
    assert actual == expected, config


def _assert_mps_catalog(workspace) -> None:
    listed = list(
        workspace.client.ai_gateway.list_model_provider_services(
            parent="schemas/ug_e2e.providers",
            view=ListModelProviderServicesRequestView.FULL,
        )
    )
    listed_by_name = {
        item.name.removeprefix("model-provider-services/"): item for item in listed if item.name
    }
    for agent, (mps_name, model) in MODEL_PROVIDER_SERVICE_FIXTURES.items():
        service = workspace.client.ai_gateway.get_model_provider_service(
            f"model-provider-services/{mps_name}"
        )
        assert service.name and service.name.endswith(mps_name), service
        config = service.config
        assert config is not None, service
        targets = config.targets or []
        assert [target.model for target in targets] == [model], service
        assert config.allow_all_targets is not True, service
        expected_type = (
            "EXTERNAL_MODEL_PROVIDER_TYPE_ANTHROPIC"
            if agent == CLAUDE
            else "EXTERNAL_MODEL_PROVIDER_TYPE_OPENAI"
        )
        provider_type = getattr(config.provider_type, "value", config.provider_type)
        assert provider_type == expected_type, config
        expected_api = "anthropic/v1/messages" if agent == CLAUDE else "openai/v1/responses"
        native_apis = [
            getattr(api, "value", api)
            for target in targets
            for api in (target.native_api_types or [])
        ]
        assert native_apis == [expected_api], targets

        matching = listed_by_name.get(mps_name)
        assert matching is not None, [item.name for item in listed]
        listed_targets = matching.config.targets if matching.config else None
        assert [target.model for target in listed_targets or []] == [model], matching


def _assert_mcp_services(workspace) -> None:
    for name in (SANDBOX_MCP_SERVICE_NAME, WEB_SEARCH_MCP_SERVICE_NAME):
        service = workspace.client.ai_gateway.get_mcp_service(f"mcp-services/{name}")
        assert service.name and service.name.endswith(name), service


def _assert_configured_files(session, workspace_url: str) -> None:
    managed_cache = json.loads(
        (session.home / ".ucode" / "managed-config.json").read_text(encoding="utf-8")
    )
    managed_config = managed_cache.get("config")
    assert isinstance(managed_config, dict), managed_cache
    _assert_cuj2_config(managed_config)

    claude_settings = session.home / ".claude" / "ucode-settings.json"
    assert claude_settings.is_file(), claude_settings
    settings = json.loads(claude_settings.read_text(encoding="utf-8"))
    claude_env = settings.get("env")
    assert isinstance(claude_env, dict), settings
    claude_mps, claude_model = MODEL_PROVIDER_SERVICE_FIXTURES[CLAUDE]
    custom_headers = claude_env.get("ANTHROPIC_CUSTOM_HEADERS")
    assert isinstance(custom_headers, str), claude_env
    claude_headers = {}
    for line in custom_headers.splitlines():
        name, separator, value = line.partition(":")
        assert separator, line
        claude_headers[name.strip().casefold()] = value.strip()
    assert claude_headers["databricks-model-provider-service"] == claude_mps, claude_headers
    assert claude_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == claude_model, claude_env
    assert claude_env["ANTHROPIC_BASE_URL"] == workspace_url.rstrip("/") + "/ai-gateway/anthropic"

    _, codex_model = MODEL_PROVIDER_SERVICE_FIXTURES[CODEX]
    codex_path = session.home / ".codex" / "ucode.config.toml"
    assert codex_path.is_file(), codex_path
    codex_config = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    assert codex_config["model_provider"] == "Databricks", codex_config
    codex_provider = codex_config["model_providers"]["Databricks"]
    assert codex_provider["wire_api"] == "responses", codex_provider
    assert codex_provider["base_url"] == workspace_url.rstrip("/") + "/ai-gateway/codex/v1"
    assert session.workspace_state()["codex_default_model"] == codex_model, (
        session.workspace_state()
    )


def _mcp_name_listed(output: str, name: str) -> bool:
    dashed = name.replace(".", "-")
    return any(name in line or dashed in line for line in output.splitlines())


def _assert_generated_mcp_listings(session) -> None:
    for agent in (CLAUDE, CODEX):
        binary = shutil.which(agent, path=session.env["PATH"])
        assert binary, f"Required agent is not on the isolated PATH: {agent}"
        listing = session.run("mcp", "list", binary=binary, timeout=120)
        output = f"{listing.stdout}\n{listing.stderr}"
        assert _mcp_name_listed(output, SANDBOX_MCP_SERVICE_NAME), output
        assert not _mcp_name_listed(output, WEB_SEARCH_MCP_SERVICE_NAME), output


def _dict_nodes(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _dict_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dict_nodes(child)


def _call_id(mapping: dict) -> str | None:
    for key in ("id", "call_id", "callId", "tool_use_id", "toolUseId"):
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parent_transcripts(session, agent: str) -> dict[str, list[dict]]:
    return {
        path: records
        for path, records in agent_sessions(session, agent).items()
        if not is_child_session(agent, path, records)
    }


def _correlated_run_code(transcripts: dict[str, list[dict]], marker: str) -> dict:
    calls: dict[str, dict] = {}
    results: dict[str, dict] = {}
    for records in transcripts.values():
        for record in records:
            for node in _dict_nodes(record):
                event_type = str(node.get("type", "")).lower()
                identifier = _call_id(node)
                if not identifier:
                    continue
                if event_type in TOOL_CALL_EVENT_TYPES:
                    calls[identifier] = node
                elif event_type in TOOL_RESULT_EVENT_TYPES:
                    results[identifier] = node

    for identifier, call in calls.items():
        call_text = json.dumps(call, sort_keys=True, default=str).lower()
        result = results.get(identifier)
        result_text = json.dumps(result, sort_keys=True, default=str) if result else ""
        failed = any(
            node.get("is_error") is True
            or str(node.get("status", "")).lower() in {"error", "failed", "failure"}
            for node in _dict_nodes(result)
        )
        if (
            "run_code" in call_text
            and any(name in call_text for name in SANDBOX_MCP_TOOL_IDENTIFIERS)
            and marker.lower() in call_text
            and marker in result_text
            and not failed
        ):
            return {"call_id": identifier, "call": call, "result": result}
    raise AssertionError(
        f"No successful sandbox run_code call/result carried marker {marker!r}; "
        f"calls={list(calls)} results={list(results)}"
    )


def _task_complete(session, agent: str, marker: str) -> bool:
    transcripts = _parent_transcripts(session, agent)
    if not any(
        marker in answer
        for records in transcripts.values()
        for answer in assistant_answers(agent, records)
    ):
        return False
    try:
        _correlated_run_code(transcripts, marker)
    except AssertionError:
        return False
    return True


def _assert_task_evidence(session, agent: str, marker: str) -> None:
    transcripts = _parent_transcripts(session, agent)
    tool_evidence = _correlated_run_code(transcripts, marker)
    answers = [
        answer for records in transcripts.values() for answer in assistant_answers(agent, records)
    ]
    assert any(marker in answer for answer in answers), answers
    session.record(
        f"cuj2-{agent}-evidence",
        {"marker": marker, "tool": tool_evidence, "answers": answers},
    )


def _agent_prompt(marker: str) -> str:
    return (
        "Complete this task using the configured model and MCP; do not answer from the prompt. "
        f"Call the configured `run_code` MCP tool with a Python program that prints the exact "
        f"marker `{marker}`. Wait for the real tool result. "
        f"Final answer must contain the marker `{marker}`, with no invented tool output."
    )


def _assert_inference_request(recorder, request, *, provider: str, model: str, marker: str) -> None:
    assert request.headers["databricks-model-provider-service"] == provider
    assert request.payload["model"] == model
    assert marker.encode() in request.body, request.payload
    response = recorder.response_for(request, timeout=240)
    assert response.status_code == 200, response.status_code
    assert response.body, "Inference response was empty"


class TestCuj2MpsExplicitMcp(BaseCujTest):
    WORKSPACE_URL = WORKSPACE_URL

    def test_cuj_configuration(self, cuj):
        """Scenario: configure ug from the preconfigured two-agent MPS/MCP workspace.

        Expected: the read-only CodingAgentConfig selects the two exact MPS resources and sandbox
        MCP; both native MPS APIs advertise their selected model, generated agent settings use
        the exact provider/model values, and agent MCP listings exclude web_search.
        """
        session, workspace, _ = cuj
        published = workspace.config()
        _assert_cuj2_config(published)
        _assert_mps_catalog(workspace)
        _assert_mcp_services(workspace)

        configured = session.run(
            "configure",
            "--workspace",
            workspace.url,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=300,
        )
        assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
        _assert_configured_files(session, workspace.url)
        _assert_generated_mcp_listings(session)

    def test_cuj_codex_inference(self, cuj):
        """Scenario: configure through the recorder and complete a real Codex MCP task.

        Expected: Codex sends a Responses request for gpt-5-nano with the
        ug_e2e.providers.openai target header and receives its paired HTTP 200 response; its
        parent transcript contains a correlated sandbox run_code call/result and final marker.
        """
        session, workspace, recorder = cuj
        published = workspace.config()
        _assert_cuj2_config(published)
        _assert_mps_catalog(workspace)
        _assert_mcp_services(workspace)

        session.configure(
            [
                "configure",
                "--workspace",
                recorder.url,
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ]
        )
        _assert_configured_files(session, recorder.url)
        _assert_generated_mcp_listings(session)

        marker = f"CUJ2-CODEX-{uuid.uuid4().hex}"
        checkpoint = recorder.checkpoint()
        with AgentTerminal(session, CODEX, [str(session.binary), CODEX], "cuj2-codex") as tui:
            tui.boot()
            tui.submit(_agent_prompt(marker))

            def completed(screen):
                assert_no_terminal_api_error(screen)
                assert "Do you want to proceed?" not in screen, screen
                return _task_complete(session, CODEX, marker)

            tui.wait_for(completed, "completed Codex MCP task", timeout=240)
            tui.exit_normally()

        request = recorder.expect_request(
            method="POST",
            path="/ai-gateway/codex/v1/responses",
            after=checkpoint,
            timeout=240,
        )
        _assert_inference_request(
            recorder,
            request,
            provider=MODEL_PROVIDER_SERVICE_FIXTURES[CODEX][0],
            model=MODEL_PROVIDER_SERVICE_FIXTURES[CODEX][1],
            marker=marker,
        )
        _assert_task_evidence(session, CODEX, marker)

    def test_cuj_claude_inference(self, cuj):
        """Scenario: configure through the recorder and complete a real Claude MCP task.

        Expected: Claude sends a Messages request for claude-haiku-4-5-20251001 with the
        ug_e2e.providers.anthropic target header and receives its paired HTTP 200 response; its
        parent transcript contains a correlated sandbox run_code call/result and final marker.
        """
        session, workspace, recorder = cuj
        published = workspace.config()
        _assert_cuj2_config(published)
        _assert_mps_catalog(workspace)
        _assert_mcp_services(workspace)

        session.configure(
            [
                "configure",
                "--workspace",
                recorder.url,
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ]
        )
        _assert_configured_files(session, recorder.url)
        _assert_generated_mcp_listings(session)

        marker = f"CUJ2-CLAUDE-{uuid.uuid4().hex}"
        checkpoint = recorder.checkpoint()
        with AgentTerminal(session, CLAUDE, [str(session.binary), CLAUDE], "cuj2-claude") as tui:
            tui.boot()
            tui.submit(_agent_prompt(marker))

            def completed(screen):
                assert_no_terminal_api_error(screen)
                assert "Do you want to proceed?" not in screen, screen
                return _task_complete(session, CLAUDE, marker)

            tui.wait_for(completed, "completed Claude MCP task", timeout=240)
            tui.exit_normally()

        request = recorder.expect_request(
            method="POST",
            path="/ai-gateway/anthropic/v1/messages",
            after=checkpoint,
            timeout=240,
        )
        _assert_inference_request(
            recorder,
            request,
            provider=MODEL_PROVIDER_SERVICE_FIXTURES[CLAUDE][0],
            model=MODEL_PROVIDER_SERVICE_FIXTURES[CLAUDE][1],
            marker=marker,
        )
        _assert_task_evidence(session, CLAUDE, marker)
