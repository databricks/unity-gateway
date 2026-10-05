"""End-to-end sentinel for the published managed Claude and Codex configuration.

The selected managed workspace publishes one static configuration for both agents. This journey
fetches that configuration through one public ``ug configure`` command, checks the generated files,
then runs real tasks on the bare managed default and explicit Claude/Codex selections.
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
import uuid

import pytest
from base import BaseCujTest
from utils.evidence import FileTask
from utils.model_discovery import claude_model_in_picker
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
CLAUDE_PICKER_LABELS = ("Claude Opus 4.8", "Claude Sonnet 4.6", "Claude Haiku 4.5")
TRACE_WAIT_SECONDS = 360
TRACE_POLL_SECONDS = 10


def _claude_models_visible(text: str) -> bool:
    return all(
        claude_model_in_picker(text, model, label)
        for model, label in zip(CLAUDE_MODELS, CLAUDE_PICKER_LABELS, strict=True)
    )


def _claude_custom_model_ids(screen: str) -> list[str]:
    return re.findall(
        r"(?m)^\s*(?:[❯›>]\s*)?\d+\.\s+.*Custom model \(([^)]+)\)",
        screen,
    )


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


def _assert_generated_configs(session, claude_headers: dict[str, str]) -> None:
    claude_settings_path = session.home / ".claude" / "ucode-settings.json"
    claude_settings = json.loads(claude_settings_path.read_text())
    assert claude_settings["availableModels"] == CLAUDE_MODELS, claude_settings
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
    """Count distinct marker/model pairs in both agents' trace attribute layouts."""
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
    """Return the model name emitted by the gateway's native client span."""
    return re.sub(r"\[(?:1m|200k)\]$", "", model).removeprefix("system.ai.")


def _trace_client_pair_count(
    workspace: str,
    bearer: str,
    warehouse_id: str,
    table: str,
    pairs: list[tuple[str, str, str]],
) -> int:
    """Count expected native gateway client models for the task markers."""
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


class TestCujManagedConfig(BaseCujTest):
    WORKSPACE_URL = "https://dbc-135c115c-c255.cloud.databricks.com"

    def test_cuj_managed_config_sentinel(self, live_session):
        """Scenario: configure one published two-agent policy and run both agents.

        Expected: no agent selector; configured models, defaults, tracing and headers in generated
        files; six completed tasks and matching agent inference plus native gateway client spans.
        Auxiliary client calls for a task may use another model. Gateway receipt of headers is not
        asserted here.
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
        _assert_generated_configs(session, claude_headers)
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

        bearer = session.env["DATABRICKS_BEARER"]
        trace_table = resolve_trace_table(workspace, bearer)
        warehouse_id = os.environ.get("UG_INTEGRATION_WAREHOUSE_ID", "").strip()
        warehouse_id = warehouse_id or resolve_warehouse_id(workspace, bearer)
        run_id = uuid.uuid4().hex
        trace_pairs: list[tuple[str, str, str]] = []

        root_task = FileTask(session)
        root_marker = f"ug-managed-sentinel-{run_id}-root"
        trace_pairs.append((root_marker, CLAUDE_DEFAULTS["default_model"], "resource"))
        session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={root_marker}"
        with AgentTerminal(
            session, "claude", [str(session.binary)], "managed-sentinel-root"
        ) as tui:
            tui.boot()
            tui.submit(f"{root_task.prompt} Trace marker: {root_marker}")
            tui.wait_for_task(root_task)
            picker_screen = tui.open_model_picker(model_visible=_claude_models_visible)
            assert _claude_custom_model_ids(picker_screen) == CLAUDE_MODELS, picker_screen
            tui.exit_normally()
        root_task.assert_completed(session, "claude")

        claude_opus_task = FileTask(session)
        claude_opus_marker = f"ug-managed-sentinel-{run_id}-claude-opus"
        trace_pairs.append(
            (claude_opus_marker, CLAUDE_DEFAULTS["default_opus_model"] + "[1m]", "resource")
        )
        session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={claude_opus_marker}"
        with AgentTerminal(
            session,
            "claude",
            [str(session.binary), "claude", "--model", "opus"],
            "managed-sentinel-claude-opus",
        ) as tui:
            tui.boot()
            tui.submit(f"{claude_opus_task.prompt} Trace marker: {claude_opus_marker}")
            tui.wait_for_task(claude_opus_task)
            tui.exit_normally()
        claude_opus_task.assert_completed(session, "claude")

        claude_sonnet_task = FileTask(session)
        claude_sonnet_marker = f"ug-managed-sentinel-{run_id}-claude-sonnet"
        trace_pairs.append(
            (claude_sonnet_marker, CLAUDE_DEFAULTS["default_sonnet_model"] + "[1m]", "resource")
        )
        session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={claude_sonnet_marker}"
        with AgentTerminal(
            session,
            "claude",
            [str(session.binary), "claude", "--model", "sonnet"],
            "managed-sentinel-claude-sonnet",
        ) as tui:
            tui.boot()
            tui.submit(f"{claude_sonnet_task.prompt} Trace marker: {claude_sonnet_marker}")
            tui.wait_for_task(claude_sonnet_task)
            tui.exit_normally()
        claude_sonnet_task.assert_completed(session, "claude")

        claude_haiku_task = FileTask(session)
        claude_haiku_marker = f"ug-managed-sentinel-{run_id}-claude-haiku"
        trace_pairs.append(
            (claude_haiku_marker, CLAUDE_DEFAULTS["default_haiku_model"], "resource")
        )
        session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={claude_haiku_marker}"
        with AgentTerminal(
            session,
            "claude",
            [str(session.binary), "claude", "--model", "haiku"],
            "managed-sentinel-claude-haiku",
        ) as tui:
            tui.boot()
            tui.submit(f"{claude_haiku_task.prompt} Trace marker: {claude_haiku_marker}")
            tui.wait_for_task(claude_haiku_task)
            tui.exit_normally()
        claude_haiku_task.assert_completed(session, "claude")

        codex_default_task = FileTask(session)
        codex_default_marker = f"ug-managed-sentinel-{run_id}-codex-default"
        trace_pairs.append((codex_default_marker, CODEX_DEFAULT, "span"))
        session.env.pop("OTEL_RESOURCE_ATTRIBUTES", None)
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
            "managed-sentinel-codex-default",
        ) as tui:
            tui.boot()
            tui.submit(f"{codex_default_task.prompt} Trace marker: {codex_default_marker}")
            tui.wait_for_task(codex_default_task)
            tui.exit_normally()
        codex_default_task.assert_completed(session, "codex")

        codex_luna_task = FileTask(session)
        codex_luna_marker = f"ug-managed-sentinel-{run_id}-codex-luna"
        trace_pairs.append((codex_luna_marker, CODEX_LUNA, "span"))
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
            "managed-sentinel-codex-luna",
        ) as tui:
            tui.boot()
            tui.submit(f"{codex_luna_task.prompt} Trace marker: {codex_luna_marker}")
            tui.wait_for_task(codex_luna_task)
            tui.exit_normally()
        codex_luna_task.assert_completed(session, "codex")

        _wait_for_trace_pairs(workspace, bearer, warehouse_id, trace_table, trace_pairs, session)
