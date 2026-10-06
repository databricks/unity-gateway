"""End-to-end journeys for managed Claude and Codex configuration."""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
import uuid

import pytest
from base import BaseCujTest
from helpers.tui_request_recorder import TuiRequestRecorder
from utils.evidence import FileTask
from utils.sql import query_count, resolve_trace_table, resolve_warehouse_id
from utils.terminal import AgentTerminal

CLAUDE_MODELS = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
CLAUDE_DEFAULTS = {
    "default_model": "system.ai.claude-sonnet-4-6",
    "default_opus_model": "system.ai.claude-opus-4-8",
    "default_sonnet_model": "system.ai.claude-sonnet-4-6",
    "default_haiku_model": "system.ai.claude-haiku-4-5",
}
CODEX_MODELS = ["system.ai.gpt-5-6-sol", "system.ai.gpt-5-6-luna"]
CODEX_DEFAULT = CODEX_MODELS[0]
CODEX_LUNA = CODEX_MODELS[1]
EXPECTED_HEADER_NAMES = {"x-ug-e2e-run", "x-ug-e2e-agent"}
CLAUDE_REQUEST_PATH = "/ai-gateway/anthropic/v1/messages"
CODEX_REQUEST_PATH = "/ai-gateway/codex/v1/responses"
TRACE_WAIT_SECONDS = 360
TRACE_POLL_SECONDS = 10


def _claude_models_visible(text: str) -> bool:
    return all(model in text for model in CLAUDE_MODELS)


def _claude_picker_visible(text: str) -> bool:
    selected_model_row = any(
        line.lstrip().startswith(("❯", "›", ">")) and any(model in line for model in CLAUDE_MODELS)
        for line in text.splitlines()
    )
    return selected_model_row and _claude_models_visible(text)


def _assert_inference_requests(
    recorder: TuiRequestRecorder,
    checkpoint: int,
    path: str,
    marker: str,
    run_id: str,
    agent: str,
    expected_model: str,
) -> None:
    """Check marked requests, headers, models, and tool follow-ups."""
    observed = []
    after = checkpoint
    while True:
        try:
            request = recorder.expect_request(method="POST", after=after, timeout=0.2)
        except AssertionError as exc:
            assert str(exc).startswith("Timed out waiting for TUI request:"), str(exc)
            break
        observed.append(request)
        after = request.sequence

    requests = [request for request in observed if request.path == path]
    assert len(requests) >= 2, [(request.sequence, request.path) for request in observed]
    served_models = []
    marked_requests = 0
    for request in requests:
        assert request.headers["x-ug-e2e-run"] == run_id, request.headers
        # AIGTWY-4876: Requests retain the configured agent header.
        assert request.headers["x-ug-e2e-agent"] == agent, request.headers
        payload = request.payload
        assert isinstance(payload, dict), type(payload)
        assert isinstance(payload.get("model"), str), sorted(payload)
        if marker in json.dumps(payload):
            marked_requests += 1
            if recorder.response_for(request).status_code == 200:
                served_models.append(payload["model"])
    assert marked_requests >= 2, marked_requests
    expected = re.sub(r"\[(?:1m|200k)\]$", "", expected_model)
    assert (
        sum(re.sub(r"\[(?:1m|200k)\]$", "", model) == expected for model in served_models) >= 2
    ), served_models


def _managed_config(session) -> dict:
    cache_path = session.home / ".ucode" / "managed-config.json"
    cache = json.loads(cache_path.read_text())
    raw = cache.get("config")
    assert isinstance(raw, dict), cache
    return raw


def _managed_entries(raw: dict) -> dict[str, dict]:
    entries = raw.get("enabled_agents")
    assert isinstance(entries, list), raw
    assert len(entries) == 2, raw
    result = {
        entry.get("agent"): entry.get("config")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("config"), dict)
    }
    assert set(result) == {"CODING_AGENT_CLAUDE_CODE", "CODING_AGENT_CODEX"}, raw
    return result


def _normalized_headers(headers: object) -> dict[str, str]:
    assert isinstance(headers, dict), headers
    assert all(isinstance(name, str) for name in headers), headers
    result = {name.casefold(): value for name, value in headers.items()}
    assert set(result) == EXPECTED_HEADER_NAMES, headers
    assert all(isinstance(value, str) and value for value in result.values()), headers
    return result


def _managed_header_subset(headers: object) -> dict[str, str]:
    assert isinstance(headers, dict), headers
    assert all(isinstance(name, str) for name in headers), headers
    normalized = {name.casefold(): value for name, value in headers.items()}
    assert EXPECTED_HEADER_NAMES <= set(normalized), headers
    assert all(
        isinstance(normalized[name], str) and normalized[name] for name in EXPECTED_HEADER_NAMES
    )
    return {name: normalized[name] for name in EXPECTED_HEADER_NAMES}


def _claude_header_lines(value: object) -> dict[str, str]:
    assert isinstance(value, str), value
    headers = {}
    for line in value.splitlines():
        name, separator, header_value = line.partition(":")
        if separator:
            headers[name.strip().casefold()] = header_value.strip()
    return _managed_header_subset(headers)


def _assert_published_config(raw: dict, entries: dict[str, dict]) -> dict[str, str]:
    assert raw["default_agent"] == "CODING_AGENT_CLAUDE_CODE", raw
    claude = entries["CODING_AGENT_CLAUDE_CODE"]
    codex = entries["CODING_AGENT_CODEX"]

    assert claude["smart_routing"] == {"enabled": False}, claude
    assert claude["models"] == {"model_services": CLAUDE_MODELS}, claude
    assert claude["default_models"] == CLAUDE_DEFAULTS, claude
    assert claude["tracing"] == {"enabled": True}, claude
    claude_headers = _normalized_headers(claude["http_headers"])
    assert claude_headers["x-ug-e2e-agent"] == "claude", claude_headers

    assert codex["smart_routing"] == {"enabled": False}, codex
    assert codex["models"] == {"model_services": CODEX_MODELS}, codex
    assert codex["default_models"] == {"default_model": CODEX_DEFAULT}, codex
    assert codex["tracing"] == {"enabled": True}, codex
    codex_headers = _normalized_headers(codex["http_headers"])
    assert codex_headers["x-ug-e2e-agent"] == "codex", codex_headers
    assert codex_headers["x-ug-e2e-run"] == claude_headers["x-ug-e2e-run"], (
        claude_headers,
        codex_headers,
    )
    return claude_headers


def _assert_generated_configs(session, claude_headers: dict[str, str], workspace: str) -> None:
    claude_settings_path = session.home / ".claude" / "ucode-settings.json"
    claude_settings = json.loads(claude_settings_path.read_text())
    # AIGTWY-4876: Pickers expose exactly the configured model list.
    assert claude_settings["availableModels"] == CLAUDE_MODELS, claude_settings
    assert claude_settings["enforceAvailableModels"] is True, claude_settings
    claude_picker = claude_settings["modelPicker"]
    assert claude_picker["replaceBuiltInOptions"] is True, claude_settings
    assert claude_picker["options"] == [
        {"model": model, "label": label}
        for model, label in zip(
            CLAUDE_MODELS,
            ("Claude Opus 4.8", "Claude Sonnet 4.6", "Claude Haiku 4.5"),
            strict=True,
        )
    ], claude_settings
    claude_env = claude_settings["env"]
    assert claude_env["ANTHROPIC_BASE_URL"] == f"{workspace}/ai-gateway/anthropic", claude_settings
    assert claude_env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == CLAUDE_MODELS[0] + "[1m]", claude_settings
    assert claude_env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == (CLAUDE_MODELS[1] + "[1m]"), (
        claude_settings
    )
    assert claude_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == CLAUDE_MODELS[2], claude_settings
    assert _claude_header_lines(claude_env["ANTHROPIC_CUSTOM_HEADERS"]) == claude_headers, (
        claude_settings
    )
    assert claude_env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1", claude_settings
    assert claude_env["OTEL_TRACES_EXPORTER"] == "otlp", claude_settings
    assert claude_env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"].endswith(
        "/ai-gateway/otel/v1/traces"
    ), claude_settings

    codex_profile_path = session.home / ".codex" / "ucode.config.toml"
    codex_profile = tomllib.loads(codex_profile_path.read_text())
    codex_provider = codex_profile["model_providers"]["Databricks"]
    assert codex_provider["base_url"] == f"{workspace}/ai-gateway/codex/v1", codex_profile
    assert _managed_header_subset(codex_provider["http_headers"]) == {
        "x-ug-e2e-run": claude_headers["x-ug-e2e-run"],
        "x-ug-e2e-agent": "codex",
    }, codex_profile
    codex_catalog_path = session.home / ".ucode" / "codex-model-catalog.json"
    assert codex_profile["model_catalog_json"] == str(codex_catalog_path), codex_profile
    codex_catalog = json.loads(codex_catalog_path.read_text())
    assert [
        model["slug"] for model in codex_catalog["models"] if model.get("visibility") == "list"
    ] == CODEX_MODELS, codex_catalog
    codex_app_config = tomllib.loads((session.home / ".codex" / "config.toml").read_text())
    assert codex_app_config["model_catalog_json"] == str(codex_catalog_path), codex_app_config


def _trace_pair_count(
    workspace: str,
    bearer: str,
    warehouse_id: str,
    table: str,
    pairs: list[tuple[str, str, str]],
) -> int:
    """Count trace marker/model pairs."""
    clauses = []
    parameters = []
    for index, (marker, model, _marker_source) in enumerate(pairs):
        clauses.append(
            "(marker_source = :marker_source_"
            f"{index} AND marker = :marker_{index} AND model = :model_{index})"
        )
        parameters.extend(
            [
                {
                    "name": f"marker_source_{index}",
                    "value": _marker_source,
                    "type": "STRING",
                },
                {"name": f"marker_{index}", "value": marker, "type": "STRING"},
                {"name": f"model_{index}", "value": model, "type": "STRING"},
            ]
        )

    marker_expressions = {
        "resource": "variant_get(resource.attributes, '$[\"ug_integration_marker\"]', 'STRING')",
        "span": "variant_get(attributes, '$[\"ug_integration_marker\"]', 'STRING')",
    }
    span_names = {
        "resource": "claude_code.llm_request",
        "span": "model_client.stream_responses_api",
    }
    unions = []
    for source in ("resource", "span"):
        marker_expression = marker_expressions[source]
        model_key = "gen_ai.request.model" if source == "resource" else "model"
        unions.append(
            "SELECT "
            f"'{source}' AS marker_source, "
            f"{marker_expression} AS marker, "
            f"variant_get(attributes, '$[\"{model_key}\"]', 'STRING') AS model "
            f"FROM {table} WHERE kind = 'SPAN_KIND_INTERNAL' "
            f"AND name = '{span_names[source]}' "
            "AND time > current_timestamp() - INTERVAL 90 MINUTES"
        )
    statement = (
        "SELECT COUNT(*) FROM (SELECT marker_source, marker, model FROM ("
        + " UNION ALL ".join(unions)
        + ") AS candidate_rows WHERE "
        + " OR ".join(clauses)
        + " GROUP BY marker_source, marker, model) AS observed"
    )
    return query_count(workspace, bearer, warehouse_id, statement, parameters)


def _native_gateway_model(model: str) -> str:
    """Normalize a gateway client span model."""
    return re.sub(r"\[(?:1m|200k)\]$", "", model).removeprefix("system.ai.")


def _trace_client_pair_count(
    workspace: str,
    bearer: str,
    warehouse_id: str,
    table: str,
    pairs: list[tuple[str, str, str]],
) -> int:
    """Count native client marker/model pairs."""
    input_messages = "CAST(variant_get(attributes, '$[\"gen_ai.input.messages\"]') AS STRING)"
    model = "variant_get(attributes, '$[\"gen_ai.request.model\"]', 'STRING')"
    expected_cases = []
    parameters = []
    for index, (marker, expected_model, _marker_source) in enumerate(pairs):
        expected_cases.append(
            f"WHEN input_messages LIKE concat('%', :client_marker_{index}, '%') "
            f"AND model = :client_model_{index} THEN :client_marker_{index}"
        )
        parameters.extend(
            [
                {
                    "name": f"client_marker_{index}",
                    "value": marker,
                    "type": "STRING",
                },
                {
                    "name": f"client_model_{index}",
                    "value": _native_gateway_model(expected_model),
                    "type": "STRING",
                },
            ]
        )
    statement = (
        "WITH candidates AS ("
        "SELECT "
        f"{input_messages} AS input_messages, "
        f"{model} AS model "
        f"FROM {table} "
        "WHERE kind = 'SPAN_KIND_CLIENT' "
        "AND time > current_timestamp() - INTERVAL 90 MINUTES) "
        "SELECT COUNT(DISTINCT CASE " + " ".join(expected_cases) + " END) FROM candidates"
    )
    return query_count(workspace, bearer, warehouse_id, statement, parameters)


def _wait_for_trace_pairs(
    workspace: str,
    bearer: str,
    warehouse_id: str,
    table: str,
    pairs: list[tuple[str, str, str]],
    session,
) -> None:
    deadline = time.monotonic() + TRACE_WAIT_SECONDS
    observed = 0
    observed_client = 0
    while True:
        observed = _trace_pair_count(workspace, bearer, warehouse_id, table, pairs)
        observed_client = _trace_client_pair_count(workspace, bearer, warehouse_id, table, pairs)
        if observed >= len(pairs) and observed_client >= len(pairs):
            session.record(
                "trace-query.json",
                {
                    "table": table,
                    "expected_pairs": [
                        {"marker": marker, "model": model, "marker_source": source}
                        for marker, model, source in pairs
                    ],
                    "observed_pairs": observed,
                    "observed_native_client_pairs": observed_client,
                    "native_client_model_scope": "expected model per marker; auxiliary models allowed",
                },
            )
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                "Managed sentinel trace evidence timed out: "
                f"observed {observed}/{len(pairs)} internal and "
                f"{observed_client}/{len(pairs)} native client marker/model pairs in {table}. "
                "The generated headers below are local writer evidence; the public trace table "
                "does not expose arbitrary HTTP request headers, so this failure does not claim "
                "that X-UG-E2E-* headers reached the gateway. Native client spans may also "
                "contain auxiliary model calls for the same task."
            )
        time.sleep(min(TRACE_POLL_SECONDS, remaining))


MANAGED_WORKSPACE_URL = "https://dbc-135c115c-c255.cloud.databricks.com"


class TestCujManagedConfiguration(BaseCujTest):
    WORKSPACE_URL = MANAGED_WORKSPACE_URL

    def test_cuj_managed_configuration(self, live_session):
        """Scenario: configure a fresh session against the published two-agent policy.

        Expected: configure selects both agents without a picker and writes their managed settings.
        """
        session = live_session
        workspace = self.WORKSPACE_URL
        configured_workspace = os.environ.get("UG_CUJ1_WORKSPACE", "").strip().rstrip("/")
        if configured_workspace:
            assert configured_workspace == workspace, configured_workspace
        configured = session.run(
            "configure",
            "--workspace",
            workspace,
            "--skip-upgrade",
            timeout=300,
        )
        assert "Select coding agents to configure:" not in configured.stdout, configured.stdout

        raw = _managed_config(session)
        entries = _managed_entries(raw)
        claude_headers = _assert_published_config(raw, entries)
        _assert_generated_configs(session, claude_headers, workspace)
        codex_models = session.codex_model_ids(
            ["app-server", "--listen", "stdio://"], name="managed-sentinel-codex-models"
        )
        assert codex_models == CODEX_MODELS, codex_models
        session.record(
            "managed-sentinel-wire.json",
            {
                "default_agent": raw["default_agent"],
                "agents": {
                    agent: {
                        "models": config["models"],
                        "default_models": config["default_models"],
                        "smart_routing": config["smart_routing"],
                        "tracing": config["tracing"],
                        "http_headers": sorted(config["http_headers"]),
                    }
                    for agent, config in entries.items()
                },
            },
        )


class TestCujManagedClaude(BaseCujTest):
    WORKSPACE_URL = MANAGED_WORKSPACE_URL

    def test_cuj_managed_claude(self, live_session):
        """Scenario: configure managed Claude and complete default and alias TUI tasks.

        Expected: the picker shows configured models; tasks complete with expected models and
        headers on every recorded inference request.
        """
        session = live_session
        workspace = self.WORKSPACE_URL
        with TuiRequestRecorder(workspace) as recorder:
            configured = session.run(
                "configure",
                "--workspace",
                recorder.url,
                "--skip-upgrade",
                timeout=300,
            )
            assert "Select coding agents to configure:" not in configured.stdout, configured.stdout

            raw = _managed_config(session)
            entries = _managed_entries(raw)
            claude = entries["CODING_AGENT_CLAUDE_CODE"]
            claude_headers = _assert_published_config(raw, entries)
            assert claude["models"] == {"model_services": CLAUDE_MODELS}, claude
            assert claude["default_models"] == CLAUDE_DEFAULTS, claude
            assert claude_headers["x-ug-e2e-agent"] == "claude", claude_headers

            claude_settings_path = session.home / ".claude" / "ucode-settings.json"
            settings = json.loads(claude_settings_path.read_text())
            assert settings["env"]["ANTHROPIC_BASE_URL"] == (
                f"{recorder.url}/ai-gateway/anthropic"
            ), settings
            run_id = claude_headers["x-ug-e2e-run"]

            root_task = FileTask(session)
            root_marker = f"ug-managed-claude-{uuid.uuid4().hex}-root"
            session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={root_marker}"
            root_checkpoint = recorder.checkpoint()
            with AgentTerminal(
                session, "claude", [str(session.binary)], "managed-claude-root"
            ) as tui:
                tui.boot()
                tui.submit(f"{root_task.prompt} Inference marker: {root_marker}")
                tui.wait_for_task(root_task)
                picker_screen = tui.open_model_picker(model_visible=_claude_picker_visible)
                assert _claude_picker_visible(picker_screen), picker_screen
                tui.exit_normally()
            root_task.assert_completed(session, "claude")
            _assert_inference_requests(
                recorder,
                root_checkpoint,
                CLAUDE_REQUEST_PATH,
                root_marker,
                run_id,
                "claude",
                CLAUDE_DEFAULTS["default_model"],
            )

            claude_opus_task = FileTask(session)
            claude_opus_marker = f"ug-managed-claude-{uuid.uuid4().hex}-opus"
            session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={claude_opus_marker}"
            opus_checkpoint = recorder.checkpoint()
            with AgentTerminal(
                session,
                "claude",
                [str(session.binary), "claude", "--model", "opus"],
                "managed-claude-opus",
            ) as tui:
                tui.boot()
                tui.submit(f"{claude_opus_task.prompt} Inference marker: {claude_opus_marker}")
                tui.wait_for_task(claude_opus_task)
                tui.exit_normally()
            claude_opus_task.assert_completed(session, "claude")
            _assert_inference_requests(
                recorder,
                opus_checkpoint,
                CLAUDE_REQUEST_PATH,
                claude_opus_marker,
                run_id,
                "claude",
                CLAUDE_DEFAULTS["default_opus_model"] + "[1m]",
            )

            claude_sonnet_task = FileTask(session)
            claude_sonnet_marker = f"ug-managed-claude-{uuid.uuid4().hex}-sonnet"
            session.env["OTEL_RESOURCE_ATTRIBUTES"] = (
                f"ug_integration_marker={claude_sonnet_marker}"
            )
            sonnet_checkpoint = recorder.checkpoint()
            with AgentTerminal(
                session,
                "claude",
                [str(session.binary), "claude", "--model", "sonnet"],
                "managed-claude-sonnet",
            ) as tui:
                tui.boot()
                tui.submit(f"{claude_sonnet_task.prompt} Inference marker: {claude_sonnet_marker}")
                tui.wait_for_task(claude_sonnet_task)
                tui.exit_normally()
            claude_sonnet_task.assert_completed(session, "claude")
            _assert_inference_requests(
                recorder,
                sonnet_checkpoint,
                CLAUDE_REQUEST_PATH,
                claude_sonnet_marker,
                run_id,
                "claude",
                CLAUDE_DEFAULTS["default_sonnet_model"] + "[1m]",
            )

            claude_haiku_task = FileTask(session)
            claude_haiku_marker = f"ug-managed-claude-{uuid.uuid4().hex}-haiku"
            session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={claude_haiku_marker}"
            haiku_checkpoint = recorder.checkpoint()
            with AgentTerminal(
                session,
                "claude",
                [str(session.binary), "claude", "--model", "haiku"],
                "managed-claude-haiku",
            ) as tui:
                tui.boot()
                tui.submit(f"{claude_haiku_task.prompt} Inference marker: {claude_haiku_marker}")
                tui.wait_for_task(claude_haiku_task)
                tui.exit_normally()
            claude_haiku_task.assert_completed(session, "claude")
            _assert_inference_requests(
                recorder,
                haiku_checkpoint,
                CLAUDE_REQUEST_PATH,
                claude_haiku_marker,
                run_id,
                "claude",
                CLAUDE_DEFAULTS["default_haiku_model"],
            )
            final_settings = json.loads(claude_settings_path.read_text())
            assert final_settings["env"]["ANTHROPIC_BASE_URL"] == (
                f"{recorder.url}/ai-gateway/anthropic"
            ), final_settings
            assert _claude_header_lines(final_settings["env"]["ANTHROPIC_CUSTOM_HEADERS"]) == (
                claude_headers
            ), final_settings


class TestCujManagedCodex(BaseCujTest):
    WORKSPACE_URL = MANAGED_WORKSPACE_URL

    def test_cuj_managed_codex(self, live_session):
        """Scenario: configure managed Codex and complete default and explicit model TUI tasks.

        Expected: the catalog is visible; tasks complete with expected Sol or Luna models and
        managed headers.
        """
        session = live_session
        workspace = self.WORKSPACE_URL
        with TuiRequestRecorder(workspace) as recorder:
            configured = session.run(
                "configure",
                "--workspace",
                recorder.url,
                "--skip-upgrade",
                timeout=300,
            )
            assert "Select coding agents to configure:" not in configured.stdout, configured.stdout

            raw = _managed_config(session)
            entries = _managed_entries(raw)
            codex = entries["CODING_AGENT_CODEX"]
            claude_headers = _assert_published_config(raw, entries)
            assert codex["models"] == {"model_services": CODEX_MODELS}, codex
            assert codex["default_models"] == {"default_model": CODEX_DEFAULT}, codex
            assert _normalized_headers(codex["http_headers"])["x-ug-e2e-agent"] == "codex", codex
            codex_models = session.codex_model_ids(
                ["app-server", "--listen", "stdio://"], name="managed-codex-models"
            )
            assert codex_models == CODEX_MODELS, codex_models

            profile = tomllib.loads((session.home / ".codex" / "ucode.config.toml").read_text())
            assert profile["model_providers"]["Databricks"]["base_url"] == (
                f"{recorder.url}/ai-gateway/codex/v1"
            ), profile
            run_id = claude_headers["x-ug-e2e-run"]
            session.env.pop("OTEL_RESOURCE_ATTRIBUTES", None)

            codex_default_task = FileTask(session)
            codex_default_marker = f"ug-managed-codex-{uuid.uuid4().hex}-default"
            default_checkpoint = recorder.checkpoint()
            with AgentTerminal(
                session,
                "codex",
                [
                    str(session.binary),
                    "codex",
                    "--",
                    "--config",
                    f'otel.span_attributes.ug_integration_marker="{codex_default_marker}"',
                ],
                "managed-codex-default",
            ) as tui:
                tui.boot()
                tui.submit(f"{codex_default_task.prompt} Inference marker: {codex_default_marker}")
                tui.wait_for_task(codex_default_task)
                tui.exit_normally()
            codex_default_task.assert_completed(session, "codex")
            _assert_inference_requests(
                recorder,
                default_checkpoint,
                CODEX_REQUEST_PATH,
                codex_default_marker,
                run_id,
                "codex",
                CODEX_DEFAULT,
            )

            codex_luna_task = FileTask(session)
            codex_luna_marker = f"ug-managed-codex-{uuid.uuid4().hex}-luna"
            luna_checkpoint = recorder.checkpoint()
            with AgentTerminal(
                session,
                "codex",
                [
                    str(session.binary),
                    "codex",
                    "--",
                    "--model",
                    CODEX_LUNA,
                    "--config",
                    f'otel.span_attributes.ug_integration_marker="{codex_luna_marker}"',
                ],
                "managed-codex-luna",
            ) as tui:
                tui.boot()
                tui.submit(f"{codex_luna_task.prompt} Inference marker: {codex_luna_marker}")
                tui.wait_for_task(codex_luna_task)
                tui.exit_normally()
            codex_luna_task.assert_completed(session, "codex")
            _assert_inference_requests(
                recorder,
                luna_checkpoint,
                CODEX_REQUEST_PATH,
                codex_luna_marker,
                run_id,
                "codex",
                CODEX_LUNA,
            )


class TestCujManagedTracing(BaseCujTest):
    WORKSPACE_URL = MANAGED_WORKSPACE_URL

    def test_cuj_managed_tracing(self, live_session):
        """Scenario: configure both managed agents and run uniquely marked tasks.

        Expected: both tasks complete and traces contain every marker/model pair in both span
        layouts; auxiliary native client calls may use another model.
        """
        session = live_session
        workspace = self.WORKSPACE_URL
        configured = session.run(
            "configure",
            "--workspace",
            workspace,
            "--skip-upgrade",
            timeout=300,
        )
        assert "Select coding agents to configure:" not in configured.stdout, configured.stdout

        raw = _managed_config(session)
        entries = _managed_entries(raw)
        _assert_published_config(raw, entries)
        bearer = session.env["DATABRICKS_BEARER"]
        trace_table = resolve_trace_table(workspace, bearer)
        warehouse_id = os.environ.get("UG_INTEGRATION_WAREHOUSE_ID", "").strip()
        warehouse_id = warehouse_id or resolve_warehouse_id(workspace, bearer)
        run_id = uuid.uuid4().hex
        trace_pairs: list[tuple[str, str, str]] = []

        claude_task = FileTask(session)
        claude_marker = f"ug-managed-trace-{run_id}-claude"
        trace_pairs.append((claude_marker, CLAUDE_DEFAULTS["default_model"], "resource"))
        session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={claude_marker}"
        with AgentTerminal(session, "claude", [str(session.binary)], "managed-trace-claude") as tui:
            tui.boot()
            tui.submit(f"{claude_task.prompt} Trace marker: {claude_marker}")
            tui.wait_for_task(claude_task)
            tui.exit_normally()
        claude_task.assert_completed(session, "claude")

        codex_task = FileTask(session)
        codex_marker = f"ug-managed-trace-{run_id}-codex"
        trace_pairs.append((codex_marker, CODEX_DEFAULT, "span"))
        session.env.pop("OTEL_RESOURCE_ATTRIBUTES", None)
        with AgentTerminal(
            session,
            "codex",
            [
                str(session.binary),
                "codex",
                "--",
                "--config",
                f'otel.span_attributes.ug_integration_marker="{codex_marker}"',
            ],
            "managed-trace-codex",
        ) as tui:
            tui.boot()
            tui.submit(f"{codex_task.prompt} Trace marker: {codex_marker}")
            tui.wait_for_task(codex_task)
            tui.exit_normally()
        codex_task.assert_completed(session, "codex")

        _wait_for_trace_pairs(workspace, bearer, warehouse_id, trace_table, trace_pairs, session)
