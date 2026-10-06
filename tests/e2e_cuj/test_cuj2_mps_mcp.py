"""CUJ2: configure two real agents from per-agent MPS resources with one MCP."""

from __future__ import annotations

import json
import tomllib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from databricks.sdk.service.catalog import ListModelProviderServicesRequestView

from .base import BaseCujTest
from .helpers import (
    McpRpc,
    WorkspaceApi,
    correlated_tool_evidence,
    listed_mcp_names,
    mcp_tools,
    structured_final_answers,
)
from .helpers.tui_request_recorder import TuiRequestRecorder

WORKSPACE_URL = "https://dbc-0dcf95cf-e357.cloud.databricks.com"
AGENT_MPS: dict[str, tuple[str, str]] = {
    "claude": ("ug_e2e.providers.anthropic", "claude-haiku-4-5-20251001"),
    "codex": ("ug_e2e.providers.openai", "gpt-5-nano"),
}
MCP_NAME = "system.ai.sandbox"
NEGATIVE_MCP_NAME = "system.ai.web_search"
CONFIG_PATH = "/api/ai-gateway/v2/coding-agent-configs"
CONFIG_UPDATE_MASK = "default_agent,enabled_agents,mcp_servers"
CONFIG_MUTABLE_FIELDS = ("default_agent", "enabled_agents", "mcp_servers")
AGENT_NAMES = {
    "claude": "CODING_AGENT_CLAUDE_CODE",
    "codex": "CODING_AGENT_CODEX",
}


def _config_rows(payload: dict | list) -> list[dict]:
    rows = payload.get("coding_agent_configs") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise AssertionError(f"Invalid CodingAgentConfig listing: {payload!r}")
    return rows


def _agent_config(agent: str) -> dict:
    mps_name, model = AGENT_MPS[agent]
    return {
        "agent": AGENT_NAMES[agent],
        "config": {
            "models": {"model_provider_service": mps_name},
            "default_models": {"default_model": model},
            "smart_routing": {"enabled": False},
            "tracing": {"enabled": False},
        },
    }


def _desired_config() -> dict:
    return {
        "spec_version": 1,
        "default_agent": AGENT_NAMES["codex"],
        "enabled_agents": [_agent_config("claude"), _agent_config("codex")],
        "mcp_servers": {"names": [MCP_NAME]},
    }


def _single_config(api: WorkspaceApi) -> dict | None:
    rows = _config_rows(api.json("GET", CONFIG_PATH))
    if len(rows) > 1:
        raise AssertionError(f"Workspace has more than one CodingAgentConfig: {rows!r}")
    return rows[0] if rows else None


def _config_resource_path(name: str) -> str:
    return f"{CONFIG_PATH}/{name.removeprefix('coding-agent-configs/').strip('/')}"


def _ensure_single_config(api: WorkspaceApi, desired: dict) -> dict:
    original = _single_config(api)
    if original is None:
        api.json("POST", CONFIG_PATH, body=desired)
    else:
        name = original.get("name")
        if not isinstance(name, str) or not name:
            raise AssertionError(f"Existing CodingAgentConfig has no resource name: {original!r}")
        api.json(
            "PATCH",
            _config_resource_path(name),
            query={"update_mask": CONFIG_UPDATE_MASK},
            body={**desired, "name": name},
        )
    updated = _single_config(api)
    if updated is None:
        raise AssertionError("Create/update did not leave a CodingAgentConfig")
    return updated


def _restore_config(api: WorkspaceApi, original: dict | None) -> None:
    if original is None:
        created = _single_config(api)
        if created is None:
            return
        name = created.get("name")
        if not isinstance(name, str) or not name:
            raise AssertionError(f"Temporary CodingAgentConfig has no resource name: {created!r}")
        api.request("DELETE", _config_resource_path(name))
        remaining = _single_config(api)
        if remaining is not None:
            raise AssertionError(f"Temporary CodingAgentConfig was not deleted: {remaining!r}")
        return

    name = original.get("name")
    if not isinstance(name, str) or not name:
        raise AssertionError(f"Original CodingAgentConfig has no resource name: {original!r}")
    body = {
        key: original[key] for key in ("spec_version", *CONFIG_MUTABLE_FIELDS) if key in original
    }
    body["name"] = name
    api.json(
        "PATCH",
        _config_resource_path(name),
        query={"update_mask": CONFIG_UPDATE_MASK},
        body=body,
    )
    restored = _single_config(api)
    if restored is None:
        raise AssertionError("Original CodingAgentConfig was not restored")
    for field in CONFIG_MUTABLE_FIELDS:
        assert restored.get(field) == original.get(field), (
            f"CodingAgentConfig field {field!r} was not restored: "
            f"expected {original.get(field)!r}, got {restored.get(field)!r}"
        )


@contextmanager
def _published_config(api: WorkspaceApi) -> Iterator[dict]:
    original = _single_config(api)
    try:
        yield _ensure_single_config(api, _desired_config())
    finally:
        _restore_config(api, original)


def _assert_published_config(config: dict) -> None:
    assert config.get("default_agent") == AGENT_NAMES["codex"], config
    assert config.get("mcp_servers") == {"names": [MCP_NAME]}, config

    entries = config.get("enabled_agents")
    assert isinstance(entries, list) and len(entries) == 2, config
    by_agent = {entry.get("agent"): entry for entry in entries if isinstance(entry, dict)}
    assert set(by_agent) == set(AGENT_NAMES.values()), config
    for agent, agent_name in AGENT_NAMES.items():
        mps_name, model = AGENT_MPS[agent]
        entry_config = by_agent[agent_name].get("config")
        assert isinstance(entry_config, dict), by_agent[agent_name]
        assert entry_config.get("models") == {"model_provider_service": mps_name}, entry_config
        assert entry_config.get("default_models") == {"default_model": model}, entry_config
        assert entry_config.get("smart_routing") == {"enabled": False}, entry_config
        assert entry_config.get("tracing") == {"enabled": False}, entry_config


def _assert_mps_catalog(workspace_client) -> None:
    listed = list(
        workspace_client.ai_gateway.list_model_provider_services(
            parent="schemas/ug_e2e.providers",
            view=ListModelProviderServicesRequestView.FULL,
        )
    )
    listed_by_name = {
        item.name.removeprefix("model-provider-services/"): item for item in listed if item.name
    }
    for agent, (mps_name, model) in AGENT_MPS.items():
        service = workspace_client.ai_gateway.get_model_provider_service(
            f"model-provider-services/{mps_name}"
        )
        assert service.name and service.name.endswith(mps_name), service
        config = service.config
        assert config is not None, service
        targets = config.targets or []
        models = [target.model for target in targets]
        assert models == [model], service
        assert config.allow_all_targets is not True, service
        expected_type = (
            "EXTERNAL_MODEL_PROVIDER_TYPE_ANTHROPIC"
            if agent == "claude"
            else "EXTERNAL_MODEL_PROVIDER_TYPE_OPENAI"
        )
        provider_type = getattr(config.provider_type, "value", config.provider_type)
        assert provider_type == expected_type, config
        expected_api = "anthropic/v1/messages" if agent == "claude" else "openai/v1/responses"
        assert [api for target in targets for api in (target.native_api_types or [])] == [
            expected_api
        ], targets

        matching = listed_by_name.get(mps_name)
        assert matching is not None, [item.name for item in listed]
        listed_targets = matching.config.targets if matching.config else None
        assert [target.model for target in listed_targets or []] == [model], matching


def _assert_mcp_prerequisite(workspace_client, api: WorkspaceApi) -> None:
    sandbox = workspace_client.ai_gateway.get_mcp_service(f"mcp-services/{MCP_NAME}")
    web_search = workspace_client.ai_gateway.get_mcp_service(f"mcp-services/{NEGATIVE_MCP_NAME}")
    assert sandbox.name and sandbox.name.endswith(MCP_NAME), sandbox
    assert web_search.name and web_search.name.endswith(NEGATIVE_MCP_NAME), web_search

    endpoint = f"/ai-gateway/mcp-services/{MCP_NAME}"
    rpc = McpRpc(api, endpoint)
    initialize = rpc.request(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "ug-cuj2", "version": "1"},
        },
    )
    assert isinstance(initialize.get("result"), dict), initialize
    rpc.notify("notifications/initialized")
    tools = mcp_tools(rpc.request("tools/list"))
    run_code = [tool for tool in tools if tool.get("name") == "run_code"]
    assert len(run_code) == 1, tools


def _assert_configured_files(session, workspace: str) -> None:
    managed_cache = json.loads(
        (session.home / ".ucode" / "managed-config.json").read_text(encoding="utf-8")
    )
    managed_config = managed_cache.get("config")
    assert isinstance(managed_config, dict), managed_cache
    _assert_published_config(managed_config)

    claude_settings = session.home / ".claude" / "ucode-settings.json"
    assert claude_settings.is_file(), claude_settings
    settings = json.loads(claude_settings.read_text(encoding="utf-8"))
    claude_env = settings.get("env")
    assert isinstance(claude_env, dict), settings
    claude_mps, claude_model = AGENT_MPS["claude"]
    custom_headers = claude_env.get("ANTHROPIC_CUSTOM_HEADERS")
    assert isinstance(custom_headers, str), claude_env
    claude_headers = {}
    for line in custom_headers.splitlines():
        name, separator, value = line.partition(":")
        assert separator, line
        claude_headers[name.strip().casefold()] = value.strip()
    assert claude_headers["databricks-model-provider-service"] == claude_mps, claude_headers
    assert claude_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == claude_model, claude_env
    assert claude_env["ANTHROPIC_BASE_URL"] == workspace.rstrip("/") + "/ai-gateway/anthropic"

    _, codex_model = AGENT_MPS["codex"]
    codex_path = session.home / ".codex" / "ucode.config.toml"
    assert codex_path.is_file(), codex_path
    codex_config = tomllib.loads(codex_path.read_text(encoding="utf-8"))
    assert codex_config["model_provider"] == "Databricks", codex_config
    assert codex_config["model"] == codex_model, codex_config
    codex_provider = codex_config["model_providers"]["Databricks"]
    assert codex_provider["wire_api"] == "responses", codex_provider
    assert codex_provider["base_url"] == workspace.rstrip("/") + "/ai-gateway/codex/v1"


def _agent_prompt(marker: str) -> str:
    return (
        "Complete this task using the configured model and MCP; do not answer from the prompt. "
        f"Call the configured `run_code` MCP tool with a Python program that prints the exact "
        f"marker `{marker}`. Wait for the real tool result. "
        f"Final answer must contain the marker `{marker}`, with no invented tool output."
    )


def _run_agent_task(session, agent: str, marker: str):
    prompt = _agent_prompt(marker)

    def completed(current) -> bool:
        if not current.transcript_contains(agent, marker):
            return False
        try:
            correlated_tool_evidence(current.transcripts(agent), "run_code", marker)
        except AssertionError:
            return False
        return True

    return session.run_tui(
        agent,
        prompt,
        completion=completed,
        timeout=240,
    )


@pytest.mark.cuj2
class TestCuj2MpsExplicitMcp(BaseCujTest):
    WORKSPACE_URL = WORKSPACE_URL

    def test_cuj_configuration(self, live_session):
        """Scenario: publish both agent-specific MPS targets and the sandbox MCP, then configure ug.

        Expected: the published config selects gpt-5-nano for Codex and
        claude-haiku-4-5-20251001 for Claude, with routing and tracing off;
        both native MPS APIs advertise their selected model, ug writes the two
        agent configs, and both agents list sandbox but exclude web_search.
        """
        session = live_session
        api = WorkspaceApi(self.workspace)
        _assert_mps_catalog(self.workspace)
        _assert_mcp_prerequisite(self.workspace, api)

        with _published_config(api) as published:
            _assert_published_config(published)
            configured = session.run(
                "configure",
                "--workspace",
                WORKSPACE_URL,
                "--skip-upgrade",
                timeout=300,
            )
            assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
            _assert_configured_files(session, WORKSPACE_URL)

            claude_listing = session.external("claude", "mcp", "list", timeout=120)
            claude_output = f"{claude_listing.stdout}\n{claude_listing.stderr}"
            assert listed_mcp_names(claude_output, MCP_NAME), claude_output
            assert not listed_mcp_names(claude_output, NEGATIVE_MCP_NAME), claude_output

            codex_listing = session.external("codex", "mcp", "list", timeout=120)
            codex_output = f"{codex_listing.stdout}\n{codex_listing.stderr}"
            assert listed_mcp_names(codex_output, MCP_NAME), codex_output
            assert not listed_mcp_names(codex_output, NEGATIVE_MCP_NAME), codex_output

    def test_cuj_codex_inference(self, live_session):
        """Scenario: configure ug from the dedicated workspace and complete a real Codex task.

        Expected: Codex sends a Responses request for gpt-5-nano with the
        ug_e2e.providers.openai target header and receives its paired HTTP 200
        response; its transcript contains a correlated sandbox run_code result
        and its final answer contains the task marker.
        """
        session = live_session
        api = WorkspaceApi(self.workspace)
        with _published_config(api) as published:
            _assert_published_config(published)
            with TuiRequestRecorder(WORKSPACE_URL) as recorder:
                configured = session.run(
                    "configure",
                    "--workspace",
                    recorder.url,
                    "--skip-upgrade",
                    timeout=300,
                )
                assert "Select coding agents to configure:" not in configured.stdout, (
                    configured.stdout
                )
                _assert_configured_files(session, recorder.url)

                marker = f"CUJ2-CODEX-{uuid.uuid4().hex}"
                checkpoint = recorder.checkpoint()
                result = _run_agent_task(session, "codex", marker)
                request = recorder.expect_request(
                    method="POST",
                    path="/ai-gateway/codex/v1/responses",
                    after=checkpoint,
                    timeout=240,
                )
                assert request.headers["databricks-model-provider-service"] == (
                    "ug_e2e.providers.openai"
                )
                assert request.payload["model"] == "gpt-5-nano"
                assert marker.encode() in request.body, request.payload
                response = recorder.response_for(request, timeout=240)
                assert response.status_code == 200, response.status_code
                assert response.body, "Codex inference response was empty"

                transcripts = session.transcripts("codex")
                assert transcripts, "No Codex transcript was written"
                assert correlated_tool_evidence(transcripts, "run_code", marker)
                answers = structured_final_answers("codex", result, transcripts)
                assert any(marker in answer for answer in answers), answers

    def test_cuj_claude_inference(self, live_session):
        """Scenario: configure ug from the dedicated workspace and complete a real Claude task.

        Expected: Claude sends a Messages request for
        claude-haiku-4-5-20251001 with the ug_e2e.providers.anthropic target
        header and receives its paired HTTP 200 response; its transcript
        contains a correlated sandbox run_code result and its final answer
        contains the task marker.
        """
        session = live_session
        api = WorkspaceApi(self.workspace)
        with _published_config(api) as published:
            _assert_published_config(published)
            with TuiRequestRecorder(WORKSPACE_URL) as recorder:
                configured = session.run(
                    "configure",
                    "--workspace",
                    recorder.url,
                    "--skip-upgrade",
                    timeout=300,
                )
                assert "Select coding agents to configure:" not in configured.stdout, (
                    configured.stdout
                )
                _assert_configured_files(session, recorder.url)

                marker = f"CUJ2-CLAUDE-{uuid.uuid4().hex}"
                checkpoint = recorder.checkpoint()
                result = _run_agent_task(session, "claude", marker)
                request = recorder.expect_request(
                    method="POST",
                    path="/ai-gateway/anthropic/v1/messages",
                    after=checkpoint,
                    timeout=240,
                )
                assert request.headers["databricks-model-provider-service"] == (
                    "ug_e2e.providers.anthropic"
                )
                assert request.payload["model"] == "claude-haiku-4-5-20251001"
                assert marker.encode() in request.body, request.payload
                response = recorder.response_for(request, timeout=240)
                assert response.status_code == 200, response.status_code
                assert response.body, "Claude inference response was empty"

                transcripts = session.transcripts("claude")
                assert transcripts, "No Claude transcript was written"
                assert correlated_tool_evidence(transcripts, "run_code", marker)
                answers = structured_final_answers("claude", result, transcripts)
                assert any(marker in answer for answer in answers), answers
