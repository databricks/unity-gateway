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
    FIXTURE_SUMMARY_SKILL_NAME,
    INFERENCE_PATHS,
    MODEL_PROVIDER_SERVICE_FIXTURES,
    SANDBOX_MCP_SERVICE_NAME,
    WEB_SEARCH_MCP_SERVICE_NAME,
)
from .helpers.workspace import Workspace

WORKSPACE_URL = "https://dbc-0dcf95cf-e357.cloud.databricks.com"


def expected_agent_config(agent: str) -> dict:
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
        "enabled_agents": [expected_agent_config(CLAUDE), expected_agent_config(CODEX)],
        "mcp_servers": {"names": [SANDBOX_MCP_SERVICE_NAME]},
        "skills": {"names": [FIXTURE_SUMMARY_SKILL_NAME]},
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

    codex_path = session.home / ".codex" / "ucode.config.toml"
    assert codex_path.is_file(), codex_path
    codex_config = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    assert codex_config["model_provider"] == "Databricks", codex_config
    codex_provider = codex_config["model_providers"]["Databricks"]
    assert codex_provider["wire_api"] == "responses", codex_provider
    assert codex_provider["base_url"] == workspace_url.rstrip("/") + "/ai-gateway/codex/v1"


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


def _parent_transcripts(session, agent: str) -> dict[str, list[dict]]:
    return {
        path: records
        for path, records in agent_sessions(session, agent).items()
        if not is_child_session(agent, path, records)
    }


def _task_complete(session, agent: str, marker: str) -> bool:
    transcripts = _parent_transcripts(session, agent)
    return any(
        marker in answer
        for records in transcripts.values()
        for answer in assistant_answers(agent, records)
    )


def _assert_task_evidence(session, agent: str, marker: str) -> None:
    transcripts = _parent_transcripts(session, agent)
    answers = [
        answer for records in transcripts.values() for answer in assistant_answers(agent, records)
    ]
    assert any(marker in answer for answer in answers), answers
    session.record(
        f"cuj2-{agent}-evidence",
        {"marker": marker, "answers": answers},
    )


def _agent_prompt(marker: str) -> str:
    return f"Reply with the exact marker `{marker}` and no other text. Do not use tools."


def _assert_inference_request(recorder, request, *, provider: str, model: str, marker: str) -> None:
    assert request.headers["databricks-model-provider-service"] == provider
    assert request.payload["model"] == model
    assert marker.encode() in request.body, request.payload
    response = recorder.response_for(request, timeout=240)
    assert response.status_code == 200, response.status_code
    assert response.body, "Inference response was empty"


class _Cuj2Base(BaseCujTest):
    WORKSPACE_URL = WORKSPACE_URL


class TestCuj2Configuration(_Cuj2Base):
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


class TestCuj2CodexInference(_Cuj2Base):
    def test_cuj_codex_inference(self, cuj):
        """Scenario: configure through the recorder and complete a real Codex inference.

        Expected: Codex sends a Responses request for gpt-5-nano with the
        ug_e2e.providers.openai target header and receives its paired HTTP 200 response; its
        parent transcript contains the final marker.
        """
        session, workspace, recorder = cuj
        published = workspace.config()
        _assert_cuj2_config(published)
        _assert_mps_catalog(workspace)
        _assert_mcp_services(workspace)

        recorder.configure_session(
            session,
            [
                "configure",
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ],
        )
        _assert_configured_files(session, workspace.url)
        _assert_generated_mcp_listings(session)

        recorder.prepare_launch()
        marker = f"CUJ2-CODEX-{uuid.uuid4().hex}"
        checkpoint = recorder.checkpoint()
        with AgentTerminal(session, CODEX, [str(session.binary), CODEX], "cuj2-codex") as tui:
            tui.boot()
            tui.submit(_agent_prompt(marker))

            def completed(screen):
                assert_no_terminal_api_error(screen)
                assert "Do you want to proceed?" not in screen, screen
                return _task_complete(session, CODEX, marker)

            tui.wait_for(completed, "completed Codex task", timeout=240)
            tui.exit_normally()

        request = recorder.expect_request(
            method="POST",
            path=INFERENCE_PATHS[CODEX],
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


class TestCuj2ClaudeInference(_Cuj2Base):
    def test_cuj_claude_inference(self, cuj):
        """Scenario: configure through the recorder and complete a real Claude inference.

        Expected: Claude sends a Messages request for claude-haiku-4-5-20251001 with the
        ug_e2e.providers.anthropic target header and receives its paired HTTP 200 response; its
        parent transcript contains the final marker.
        """
        session, workspace, recorder = cuj
        published = workspace.config()
        _assert_cuj2_config(published)
        _assert_mps_catalog(workspace)
        _assert_mcp_services(workspace)

        recorder.configure_session(
            session,
            [
                "configure",
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ],
        )
        _assert_configured_files(session, workspace.url)
        _assert_generated_mcp_listings(session)

        recorder.prepare_launch()
        marker = f"CUJ2-CLAUDE-{uuid.uuid4().hex}"
        checkpoint = recorder.checkpoint()
        with AgentTerminal(session, CLAUDE, [str(session.binary), CLAUDE], "cuj2-claude") as tui:
            tui.boot()
            tui.submit(_agent_prompt(marker))

            def completed(screen):
                assert_no_terminal_api_error(screen)
                return _task_complete(session, CLAUDE, marker)

            tui.wait_for(completed, "completed Claude task", timeout=240)
            tui.exit_normally()

        request = recorder.expect_request(
            method="POST",
            path=INFERENCE_PATHS[CLAUDE],
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
