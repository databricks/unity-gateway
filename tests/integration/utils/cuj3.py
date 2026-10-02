"""Black-box model-discovery evidence for the dedicated CUJ3 workspace."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .evidence import agent_sessions, assistant_answers
from .provider_catalog import parse_anthropic_provider_page, parse_codex_provider_catalog

MODEL_SCHEMA = "ug_e2e.models"
OTHER_MODEL_SCHEMA = "ug_e2e.other_models"
MODEL_HEADER = "Databricks-Model-Service-Parent-Schema"
CLAUDE_DEFAULT = f"{MODEL_SCHEMA}.claude_sonnet"
CODEX_DEFAULT = f"{MODEL_SCHEMA}.gpt_luna"
GEMINI_MODEL = f"{MODEL_SCHEMA}.gemini_flash"
CLAUDE_MODELS = frozenset({CLAUDE_DEFAULT, f"{MODEL_SCHEMA}.claude_haiku", f"{MODEL_SCHEMA}.kimi"})
CODEX_MODELS = frozenset({CODEX_DEFAULT, f"{MODEL_SCHEMA}.kimi"})
MODEL_SERVICES = CLAUDE_MODELS | CODEX_MODELS | {GEMINI_MODEL}
CLAUDE_DECOY = f"{OTHER_MODEL_SCHEMA}.claude_decoy"
CODEX_DECOY = f"{OTHER_MODEL_SCHEMA}.codex_decoy"


def claude_discovery_model_id(model_id: str) -> str:
    """Encode the exact wire ID expected by this workspace's Claude discovery feature."""
    if "claude" in model_id.lower() or "anthropic" in model_id.lower():
        return model_id
    checksum = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:8]
    return f"anthropic-aigw-{checksum}-{model_id}"


def claude_model_service_id(model_id: str) -> str:
    """Recover service identity only from the gateway's checksum-valid discovery alias."""
    alias = re.fullmatch(r"anthropic-aigw-([0-9a-f]{8})-(.+)", model_id)
    if alias is None:
        return model_id
    checksum, original = alias.groups()
    if hashlib.sha256(original.encode("utf-8")).hexdigest()[:8] != checksum:
        return model_id
    return original


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_model_service_inventory(workspace: str, token: str) -> dict[str, tuple[str, ...]]:
    """Prove every fixture exists independently of agent-compatible model lists."""
    origin = urllib.parse.urlsplit(workspace)
    assert (
        origin.scheme == "https"
        and origin.hostname
        and origin.username is None
        and origin.password is None
        and origin.path in {"", "/"}
        and not origin.query
        and not origin.fragment
    ), "Expected an HTTPS workspace origin without credentials"
    assert token, "Expected an explicit workspace bearer"
    opener = urllib.request.build_opener(NoRedirect())
    inventory: dict[str, tuple[str, ...]] = {}
    for model in sorted(MODEL_SERVICES | {CLAUDE_DECOY, CODEX_DECOY}):
        url = f"https://{origin.netloc}/api/2.1/unity-catalog/model-services/{model}"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with opener.open(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            raise AssertionError(f"Model service GET {model} returned HTTP {error.code}") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise AssertionError(
                f"Model service GET {model} failed or returned invalid JSON"
            ) from None
        assert isinstance(payload, dict), f"Model service {model} must be an object"
        config = payload.get("config")
        assert isinstance(config, dict), f"Model service {model} must have a config"
        routing = config.get("routing")
        assert isinstance(routing, dict), f"Model service {model} must have routing"
        assert routing.get("fallback") in (None, {}, {"destinations": []}), (
            f"Model service {model} must not have fallback destinations"
        )
        destinations = routing.get("destinations")
        assert isinstance(destinations, list) and len(destinations) == 1, (
            f"Model service {model} must have exactly one routing destination"
        )
        sources: list[str] = []
        for destination in destinations:
            assert isinstance(destination, dict), f"Invalid destination for {model}"
            source = destination.get("name")
            assert isinstance(source, str) and re.fullmatch(
                r"system\.ai\.[A-Za-z0-9_-]+", source
            ), f"Model service {model} must target an existing system.ai source"
            assert destination.get("destination_type") == (
                "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL"
            ), f"Unexpected destination type for {model}"
            target = destination.get("pay_per_token_config")
            assert isinstance(target, dict) and target.get("model") == f"models/{source}", (
                f"Model service {model} has inconsistent source routing"
            )
            traffic = destination.get("traffic_percentage", 100)
            assert type(traffic) is int and traffic == 100, (
                f"Model service {model} must route 100 percent of traffic to its source"
            )
            assert all(
                destination.get(field, False) is False
                for field in ("is_deleted", "is_disabled", "disabled")
            ), f"Model service {model} has a disabled or deleted destination"
            sources.append(source)
        inventory[model] = tuple(sources)
    return inventory


def codex_model_in_picker(screen: str, model_id: str, display_name: str) -> bool:
    """Match native numbered picker rows, not banners, prompt text, or footers."""
    rows = re.findall(r"(?m)^[ \t]*(?:[❯›>][ \t]*)?\d+[.)][ \t]+([^\n]+)$", screen)
    return any(_picker_row_matches(row, model_id, display_name) for row in rows)


def _picker_row_matches(label: str, model_id: str, display_name: str | None) -> bool:
    return any(
        re.search(rf"(?<![\w.-]){re.escape(candidate)}(?![\w.-])", label)
        for candidate in (model_id, display_name)
        if candidate
    )


def assert_picker_inventory(screen: str, agent: str, display_names: dict[str, str | None]) -> None:
    """Require exactly one unambiguous numbered native row per expected model."""
    assert agent in {"claude", "codex"}, agent
    assert display_names, "Expected a nonempty provider model inventory"
    rows = re.findall(r"(?m)^[ \t]*(?:[❯›>][ \t]*)?\d+[.)](?:[ \t]+([^\n]*))?$", screen)
    assert rows, f"No numbered {agent} picker rows:\n{screen}"
    observed: list[str] = []
    for label in rows:
        matched = {
            model
            for model, display_name in display_names.items()
            if _picker_row_matches(label, model, display_name)
        }
        assert len(matched) == 1, (
            f"Unmatched or ambiguous {agent} picker row {label!r}: {sorted(matched)}"
        )
        model = matched.pop()
        assert model not in observed, f"Duplicate {agent} picker model row: {model}"
        observed.append(model)
    assert set(observed) == set(display_names), {
        "agent": agent,
        "expected": sorted(display_names),
        "observed": observed,
    }


@dataclass(frozen=True)
class ParentCatalog:
    """Independently observed provider inventory, labels, and unfiltered payloads."""

    model_ids: tuple[str, ...]
    display_names: dict[str, str | None]
    payloads: tuple[dict, ...]


def fetch_codex_parent_catalog(workspace: str, token: str, parent_schema: str) -> ParentCatalog:
    """Keep the raw Codex catalog so hidden entries cannot conceal exclusions."""
    assert workspace.startswith("https://") and token and parent_schema
    url = f"{workspace.rstrip('/')}/ai-gateway/codex/v1/models"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            MODEL_HEADER: parent_schema,
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise AssertionError(f"Codex catalog GET returned HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise AssertionError("Codex catalog GET failed or returned invalid JSON") from None
    model_ids = parse_codex_provider_catalog(payload)
    display_names = {}
    for entry in payload["models"]:
        if entry.get("slug") in model_ids:
            label = entry.get("display_name")
            assert isinstance(label, str) and label.strip(), "Invalid Codex catalog display name"
            display_names[entry["slug"]] = label
    return ParentCatalog(model_ids, display_names, (payload,))


def fetch_claude_parent_catalog(workspace: str, token: str, parent_schema: str) -> ParentCatalog:
    """Read the real parent-scoped Anthropic catalog with bounded pagination."""
    assert workspace.startswith("https://") and token and parent_schema
    model_ids: list[str] = []
    display_names: dict[str, str | None] = {}
    pages: list[dict] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _page_number in range(20):
        query = {"limit": "1000"}
        if cursor is not None:
            query["after_id"] = cursor
        url = (
            f"{workspace.rstrip('/')}/ai-gateway/anthropic/v1/models?"
            f"{urllib.parse.urlencode(query)}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Anthropic-Version": "2023-06-01",
                MODEL_HEADER: parent_schema,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            raise AssertionError(f"GET {url} returned HTTP {error.code}: {error.reason}") from error
        except (urllib.error.URLError, OSError) as error:
            raise AssertionError(f"GET {url} failed: {error}") from error
        page = parse_anthropic_provider_page(payload)
        pages.append(payload)
        for model_id, display_name in page.models:
            assert model_id not in display_names, f"duplicate Claude model id: {model_id}"
            model_ids.append(model_id)
            display_names[model_id] = display_name
        if not page.has_more:
            return ParentCatalog(tuple(model_ids), display_names, tuple(pages))
        assert page.last_id is not None and page.last_id not in seen_cursors, payload
        seen_cursors.add(page.last_id)
        cursor = page.last_id
    raise AssertionError("Claude parent catalog exceeded 20 pages")


def assert_cuj3_config(config: dict) -> None:
    """Require exact model policy while allowing unrelated resource fields."""
    assert config["spec_version"] == 1, config
    assert config["default_agent"] == "CODING_AGENT_CLAUDE_CODE", config
    entries = config["enabled_agents"]
    assert isinstance(entries, list) and len(entries) == 2, config
    assert all(isinstance(entry, dict) for entry in entries), entries
    by_agent = {entry.get("agent"): entry.get("config") for entry in entries}
    expected_defaults = {
        "CODING_AGENT_CLAUDE_CODE": {
            "default_model": CLAUDE_DEFAULT,
            "default_sonnet_model": CLAUDE_DEFAULT,
        },
        "CODING_AGENT_CODEX": {"default_model": CODEX_DEFAULT},
    }
    assert set(by_agent) == set(expected_defaults), by_agent
    for agent, defaults in expected_defaults.items():
        agent_config = by_agent[agent]
        assert isinstance(agent_config, dict), agent_config
        assert agent_config.get("models") == {"unity_catalog_location": MODEL_SCHEMA}, agent_config
        assert agent_config.get("default_models") == defaults, agent_config
        for key in ("smart_routing", "tracing"):
            settings = agent_config.get(key)
            assert isinstance(settings, dict) and settings.get("enabled") is False, agent_config


def assert_persisted_config(session, workspace: str) -> dict:
    """Validate the policy persisted by public configure, never a setup precheck."""
    path = session.home / ".ucode" / "managed-config.json"
    assert path.is_file(), path
    persisted = json.loads(path.read_text())
    assert isinstance(persisted, dict) and persisted.get("workspace") == workspace, persisted
    config = persisted.get("config")
    assert isinstance(config, dict), persisted
    assert_cuj3_config(config)
    session.record("cuj3-managed-config.json", persisted)
    return config


def native_model_ids(session, agent: str, answer_value: str) -> set[str]:
    """Correlate a completed file answer with its native transcript model identity."""
    assert agent in {"claude", "codex"}, agent
    found: set[str] = set()
    for records in agent_sessions(session, agent).values():
        if agent == "claude":
            for record in records:
                if not any(answer_value in answer for answer in assistant_answers(agent, [record])):
                    continue
                message = record.get("message") or {}
                model = message.get("model")
                if isinstance(model, str) and model:
                    found.add(model.removesuffix("[1m]"))
        else:
            completed = {
                (record.get("payload") or {}).get("turn_id")
                for record in records
                if record.get("type") == "event_msg"
                and (record.get("payload") or {}).get("type") == "task_complete"
                and answer_value in ((record.get("payload") or {}).get("last_agent_message") or "")
            }
            assert all(isinstance(turn_id, str) and turn_id for turn_id in completed), completed
            for record in records:
                if record.get("type") != "turn_context":
                    continue
                payload = record.get("payload") or {}
                if payload.get("turn_id") in completed and isinstance(payload.get("model"), str):
                    found.add(payload["model"])
    return found


def assert_native_model_identity(session, agent: str, answer_value: str, expected: str) -> None:
    observed = native_model_ids(session, agent, answer_value)
    session.record(
        f"native-model-{agent}-{answer_value[:12]}.json",
        {"expected": expected, "observed": sorted(observed)},
    )
    assert observed == {expected}, {"expected": expected, "observed": sorted(observed)}
