"""Black-box evidence helpers for the real CUJ3 managed workspace."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .evidence import agent_sessions, assistant_answers
from .provider_catalog import parse_codex_provider_catalog

MODEL_SCHEMA = "ug_e2e.models"
OTHER_MODEL_SCHEMA = "ug_e2e.other_models"
MCP_SCHEMA = "ug_e2e.tools"
OTHER_MCP_SCHEMA = "ug_e2e.other_tools"
SKILL_SCHEMA = "ug_e2e.skills"
OTHER_SKILL_SCHEMA = "ug_e2e.other_skills"
MODEL_HEADER = "Databricks-Model-Service-Parent-Schema"

CLAUDE_MODELS = frozenset({f"{MODEL_SCHEMA}.claude_sonnet", f"{MODEL_SCHEMA}.claude_extra"})
CODEX_MODELS = frozenset({f"{MODEL_SCHEMA}.codex_primary", f"{MODEL_SCHEMA}.codex_extra"})
CLAUDE_DECOY = f"{OTHER_MODEL_SCHEMA}.claude_decoy"
CODEX_DECOY = f"{OTHER_MODEL_SCHEMA}.codex_decoy"
MCP_SERVERS = {
    "fixture_reader": f"{MCP_SCHEMA}.fixture_reader".replace(".", "-"),
    "fixture_metadata": f"{MCP_SCHEMA}.fixture_metadata".replace(".", "-"),
}
MCP_DECOY = f"{OTHER_MCP_SCHEMA}.fixture_decoy".replace(".", "-")
SKILLS = frozenset({"fixture-summary", "fixture-audit"})
SKILL_DECOY = "fixture-decoy"


@dataclass(frozen=True)
class ParentCatalog:
    """The model ids returned by one parent-schema API."""

    model_ids: tuple[str, ...]
    payloads: tuple[dict, ...]


def fixture_value(domain: str, run_id: str) -> str:
    """Derive the opaque value returned by the published fixture app."""
    payload = b"unity-gateway-cuj-fixture\0" + domain.encode("ascii") + b"\0" + run_id.encode()
    return hashlib.sha256(payload).hexdigest()


def _get_json(url: str, token: str, headers: dict[str, str]) -> object:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise AssertionError(f"GET {url} returned HTTP {error.code}: {error.reason}") from error
    except (urllib.error.URLError, OSError) as error:
        raise AssertionError(f"GET {url} failed: {error}") from error


def _base_url(workspace: str) -> str:
    return workspace.rstrip("/")


def fetch_claude_parent_catalog(workspace: str, token: str, parent_schema: str) -> ParentCatalog:
    """Fetch the complete Anthropic catalog through the real parent-schema API."""
    model_ids: list[str] = []
    pages: list[dict] = []
    after_id: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(20):
        query = {"limit": "1000"}
        if after_id:
            query["after_id"] = after_id
        url = (
            f"{_base_url(workspace)}/ai-gateway/anthropic/v1/models?{urllib.parse.urlencode(query)}"
        )
        payload = _get_json(url, token, {MODEL_HEADER: parent_schema})
        assert isinstance(payload, dict), payload
        pages.append(payload)
        raw_models = payload.get("data")
        assert isinstance(raw_models, list), payload
        for model in raw_models:
            assert isinstance(model, dict), model
            model_id = model.get("id")
            assert isinstance(model_id, str) and model_id, model
            assert model_id not in model_ids, f"duplicate Claude model id: {model_id}"
            model_ids.append(model_id)
        has_more = payload.get("has_more", False)
        assert isinstance(has_more, bool), payload
        if not has_more:
            break
        after_id = payload.get("last_id")
        assert isinstance(after_id, str) and after_id and after_id not in seen_cursors, payload
        seen_cursors.add(after_id)
    else:
        raise AssertionError("Claude parent catalog exceeded 20 pages")
    assert model_ids, pages
    return ParentCatalog(tuple(model_ids), tuple(pages))


def fetch_codex_parent_catalog(workspace: str, token: str, parent_schema: str) -> ParentCatalog:
    """Fetch the list-visible Codex catalog through the real parent-schema API."""
    url = f"{_base_url(workspace)}/ai-gateway/codex/v1/models"
    payload = _get_json(url, token, {MODEL_HEADER: parent_schema})
    assert isinstance(payload, dict), payload
    model_ids = parse_codex_provider_catalog(payload)
    return ParentCatalog(tuple(model_ids), (payload,))


def fetch_skill_names(workspace: str, token: str, location: str) -> frozenset[str]:
    """Read finalized skill bundle names from one Unity Catalog schema."""
    names: set[str] = set()
    page_token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(20):
        query_values = {"parent": f"schemas/{location}"}
        if page_token:
            query_values["page_token"] = page_token
        query = urllib.parse.urlencode(query_values)
        payload = _get_json(
            f"{_base_url(workspace)}/api/2.1/unity-catalog/skills?{query}", token, {}
        )
        assert isinstance(payload, dict), payload
        raw_skills = payload.get("skills")
        assert isinstance(raw_skills, list), payload
        for skill in raw_skills:
            assert isinstance(skill, dict), skill
            name = skill.get("bundle_name") or skill.get("name") or skill.get("id")
            assert isinstance(name, str) and name, skill
            names.add(name)
        next_token = payload.get("next_page_token")
        if not next_token:
            return frozenset(names)
        assert isinstance(next_token, str) and next_token not in seen_tokens, payload
        seen_tokens.add(next_token)
        page_token = next_token
    raise AssertionError("Unity Catalog skill inventory exceeded 20 pages")


def fetch_mcp_names(workspace: str, token: str, location: str) -> frozenset[str]:
    """Read the complete visible MCP service inventory for one UC schema."""
    names: set[str] = set()
    page_token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(20):
        query_values = {"parent": f"schemas/{location}", "view": "BASIC"}
        if page_token:
            query_values["page_token"] = page_token
        query = urllib.parse.urlencode(query_values)
        payload = _get_json(
            f"{_base_url(workspace)}/api/2.1/unity-catalog/mcp-services?{query}", token, {}
        )
        assert isinstance(payload, dict), payload
        raw_services = payload.get("mcp_services")
        assert isinstance(raw_services, list), payload
        for service in raw_services:
            assert isinstance(service, dict), service
            name = service.get("name")
            assert isinstance(name, str), service
            fqn = name.removeprefix("mcp-services/")
            assert fqn.startswith(f"{location}."), service
            names.add(fqn)
        next_token = payload.get("next_page_token")
        if not next_token:
            return frozenset(names)
        assert isinstance(next_token, str) and next_token not in seen_tokens, payload
        seen_tokens.add(next_token)
        page_token = next_token
    raise AssertionError("Unity Catalog MCP inventory exceeded 20 pages")


def assert_cuj3_config(config: dict) -> None:
    """Assert the published policy's exact schema pointers and model defaults."""
    assert config.get("spec_version") == 1, config
    assert config.get("default_agent") == "CODING_AGENT_CLAUDE_CODE", config
    assert config.get("mcp_servers") == {"unity_catalog_location": MCP_SCHEMA}, config
    assert config.get("skills") == {"unity_catalog_location": SKILL_SCHEMA}, config
    entries = config.get("enabled_agents")
    assert isinstance(entries, list), config
    by_agent = {entry.get("agent"): entry for entry in entries if isinstance(entry, dict)}
    assert set(by_agent) == {"CODING_AGENT_CLAUDE_CODE", "CODING_AGENT_CODEX"}, by_agent
    claude = by_agent["CODING_AGENT_CLAUDE_CODE"].get("config")
    assert isinstance(claude, dict), claude
    assert claude.get("models") == {"unity_catalog_location": MODEL_SCHEMA}, claude
    assert claude.get("default_models") == {
        "default_model": f"{MODEL_SCHEMA}.claude_sonnet",
        "default_sonnet_model": f"{MODEL_SCHEMA}.claude_sonnet",
    }, claude
    assert claude.get("smart_routing") == {"enabled": False}, claude
    assert claude.get("tracing") == {"enabled": False}, claude
    codex = by_agent["CODING_AGENT_CODEX"].get("config")
    assert isinstance(codex, dict), codex
    assert codex.get("models") == {"unity_catalog_location": MODEL_SCHEMA}, codex
    assert codex.get("default_models") == {"default_model": f"{MODEL_SCHEMA}.codex_primary"}, codex
    assert codex.get("smart_routing") == {"enabled": False}, codex
    assert codex.get("tracing") == {"enabled": False}, codex


def assert_persisted_config(session, workspace: str, published: dict) -> dict:
    """Require ug's raw persisted config to match the real control-plane response."""
    path = session.home / ".ucode" / "managed-config.json"
    assert path.is_file(), path
    persisted = json.loads(path.read_text())
    assert persisted.get("workspace") == workspace, persisted
    assert persisted.get("config") == published, (persisted, published)
    assert_cuj3_config(persisted["config"])
    session.record("cuj3-managed-config.json", persisted)
    return persisted["config"]


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    return str(value)


def native_model_ids(session, agent: str, answer_value: str) -> set[str]:
    """Extract model identity from the native agent transcript for one completed answer."""
    found: set[str] = set()
    sessions = agent_sessions(session, agent)
    for records in sessions.values():
        if agent == "claude":
            for record in records:
                if record.get("type") != "assistant":
                    continue
                message = record.get("message") or {}
                answer = _text(message.get("content"))
                model = message.get("model")
                if answer_value in answer and isinstance(model, str) and model:
                    found.add(model.removesuffix("[1m]"))
        else:
            completed = {
                (record.get("payload") or {}).get("turn_id")
                for record in records
                if record.get("type") == "event_msg"
                and (record.get("payload") or {}).get("type") == "task_complete"
                and answer_value in ((record.get("payload") or {}).get("last_agent_message") or "")
            }
            for record in records:
                if record.get("type") != "turn_context":
                    continue
                payload = record.get("payload") or {}
                if payload.get("turn_id") in completed and isinstance(payload.get("model"), str):
                    found.add(payload["model"])
    return found


def assert_native_model_identity(session, agent: str, answer_value: str, expected: str) -> None:
    observed = native_model_ids(session, agent, answer_value)
    assert expected in observed, {"expected": expected, "observed": sorted(observed)}
    session.record(
        f"native-model-{agent}-{answer_value[:12]}.json",
        {"expected": expected, "observed": sorted(observed)},
    )


def native_tool_evidence(
    session, agent: str, run_id: str, server_fragment: str, tool_name: str, expected_value: str
) -> None:
    """Require a native tool call with the requested run id and its real result."""
    calls: list[dict] = []
    results: list[str] = []
    for records in agent_sessions(session, agent).values():
        for node in _walk(records):
            kind = node.get("type")
            name = node.get("name")
            if kind in {"tool_use", "function_call", "mcp_tool_call"} and isinstance(name, str):
                arguments = node.get("input", node.get("arguments", {}))
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                if (
                    server_fragment in name
                    and tool_name in name
                    and isinstance(arguments, dict)
                    and arguments.get("run_id") == run_id
                ):
                    calls.append(node)
            if kind in {"tool_result", "function_call_output", "mcp_tool_result"}:
                results.append(_text(node.get("content", node.get("output", ""))))
    assert calls, {"agent": agent, "server": server_fragment, "tool": tool_name, "run_id": run_id}
    assert any(expected_value in result for result in results), {
        "agent": agent,
        "server": server_fragment,
        "tool": tool_name,
        "expected_result": expected_value,
        "results": results,
    }
    session.record(
        f"native-tool-{agent}-{tool_name}-{run_id}.json",
        {"calls": calls, "expected_result": expected_value, "results": results},
    )


def assert_skill_transcript(session, agent: str, skill: str, domain: str, run_id: str) -> None:
    """Require a skill result and the native skill-loading event that preceded it."""
    expected = fixture_value(domain, run_id)
    sessions = agent_sessions(session, agent)
    records = [record for rows in sessions.values() for record in rows]
    answers = assistant_answers(agent, records)
    assert any(f"{skill}:" in answer and expected in answer for answer in answers), answers
    loads: list[dict] = []
    load_results: list[dict] = []
    load_ids: set[str] = set()
    for node in _walk(records):
        kind = str(node.get("type") or "").lower()
        name = str(node.get("name") or node.get("tool") or "").lower()
        serialized = json.dumps(node, sort_keys=True)
        skill_path = f"{skill}/SKILL.md"
        is_skill_load = (
            (name in {"skill", "load_skill", "loadskill"} and skill in serialized)
            or ("read" in name and skill_path in serialized)
            or (kind in {"function_call", "tool_use"} and skill_path in serialized)
            or (kind == "skill" and skill in serialized)
        )
        if is_skill_load:
            loads.append(node)
            for key in ("id", "call_id", "tool_use_id"):
                value = node.get(key)
                if isinstance(value, str):
                    load_ids.add(value)
        if kind in {"tool_result", "function_call_output", "mcp_tool_result"}:
            result_ids = {
                node[key]
                for key in ("id", "call_id", "tool_use_id")
                if isinstance(node.get(key), str)
            }
            if result_ids & load_ids or skill in serialized or skill_path in serialized:
                load_results.append(node)
    assert loads, f"native transcript did not load skill {skill}"
    assert load_results, {
        "skill": skill,
        "loads": loads,
        "answers": answers,
    }
    session.record(
        f"skill-{agent}-{skill}.json",
        {
            "skill": skill,
            "run_id": run_id,
            "expected_result": expected,
            "loads": loads,
            "load_results": load_results,
        },
    )
