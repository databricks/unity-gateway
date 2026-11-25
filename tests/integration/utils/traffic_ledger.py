"""Extract redacted, explicitly partial model usage from native agent sessions."""

import argparse
import json
from datetime import datetime
from pathlib import Path

TOKEN_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "reasoning_output_tokens",
}


def model_sets(fixtures):
    result = []
    for name in ("managed_workspace_default", "codex_smart_routing"):
        payload = json.loads((fixtures / f"{name}.json").read_text(encoding="utf-8"))
        entries = [
            item for item in payload["enabled_agents"] if item.get("agent") == "CODING_AGENT_CODEX"
        ]
        if len(entries) != 1:
            raise ValueError(f"Expected one Codex configuration in {name}")
        models = entries[0]["config"]["models"]["model_services"]
        if (
            not isinstance(models, list)
            or not models
            or not all(isinstance(model, str) and model for model in models)
        ):
            raise ValueError(f"Expected nonempty model services in {name}")
        result.append(set(models))
    return tuple(result)


def canonical_model(model, known_models):
    if not isinstance(model, str):
        return None
    if model in known_models:
        return model
    matches = [
        candidate for candidate in known_models if candidate.removeprefix("system.ai.") == model
    ]
    return matches[0] if len(matches) == 1 else None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _usage(value):
    if not isinstance(value, dict):
        return {}, False
    counts = {
        key: number
        for key, number in value.items()
        if key in TOKEN_FIELDS and type(number) is int and number >= 0
    }
    return counts, "input_tokens" in counts and "output_tokens" in counts


def native_usage(codex_sessions, claude_sessions, *, workspace, known_models):
    if workspace not in (None, "managed-primary"):
        raise ValueError("Unknown traffic-ledger workspace label")
    responses = []
    seen = {}
    unmapped = workspace is None
    for path, records in codex_sessions.items():
        contexts = {}
        for record in records:
            if not isinstance(record, dict) or record.get("type") != "turn_context":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                continue
            model = canonical_model(payload.get("model"), known_models)
            if turn_id in contexts and contexts[turn_id] != model:
                unmapped = True
            contexts[turn_id] = model
        for record in records:
            if not isinstance(record, dict) or record.get("type") != "token_usage_record":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                unmapped = True
                continue
            identifier = payload.get("response_id")
            if not isinstance(identifier, str) or not identifier:
                unmapped = True
                continue
            key = ("codex", payload.get("session_id") or path, identifier)
            model = canonical_model(contexts.get(payload.get("turn_id")), known_models)
            if key in seen:
                unmapped |= seen[key] != model
                continue
            seen[key] = model
            timestamp = _timestamp(record.get("timestamp"))
            usage, complete_usage = _usage(payload.get("usage"))
            unmapped |= (
                model is None or timestamp is None or not complete_usage or workspace is None
            )
            responses.append(
                {
                    "source": "codex-native",
                    "timestamp": timestamp,
                    "model": model,
                    "workspace": workspace,
                    "usage": usage,
                }
            )
    for path, records in claude_sessions.items():
        for record in records:
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                unmapped = True
                continue
            identifier = message.get("id")
            if not isinstance(identifier, str) or not identifier:
                unmapped = True
                continue
            key = ("claude", record.get("sessionId") or path, identifier)
            model = canonical_model(message.get("model"), known_models)
            if key in seen:
                unmapped |= seen[key] != model
                continue
            seen[key] = model
            timestamp = _timestamp(record.get("timestamp"))
            usage, complete_usage = _usage(message.get("usage"))
            unmapped |= (
                model is None or timestamp is None or not complete_usage or workspace is None
            )
            responses.append(
                {
                    "source": "claude-native",
                    "timestamp": timestamp,
                    "model": model,
                    "workspace": workspace,
                    "usage": usage,
                }
            )
    responses.sort(key=lambda response: response["timestamp"] or "")
    return {
        "coverage": "unmapped" if unmapped else "partial",
        "timestamp_semantics": "native-response-recorded",
        "responses": responses,
    }


def record_case_usage(session, *, workspace):
    from .evidence import agent_sessions

    fixtures = Path(__file__).resolve().parents[2] / "fixtures/managed_config"
    desktop_models, smart_models = model_sets(fixtures)
    known_models = desktop_models | smart_models
    for path in fixtures.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        for agent in payload.get("enabled_agents", []):
            config = agent.get("config") or {}
            models = config.get("models") or {}
            for model in models.get("model_services", []):
                if isinstance(model, str):
                    known_models.add(model)
    result = native_usage(
        agent_sessions(session, "codex"),
        agent_sessions(session, "claude"),
        workspace=workspace,
        known_models=known_models,
    )
    session.record("model-ledger.json", result)


def check_model_separation(responses, *, lane, desktop_models, smart_models):
    if lane not in ("desktop", "smart"):
        raise ValueError("Unknown model-guard lane")
    if not responses:
        raise ValueError("No observed model responses")
    known_models = desktop_models | smart_models
    forbidden = desktop_models if lane == "smart" else smart_models
    for response in responses:
        model = canonical_model(response.get("model"), known_models)
        if response.get("workspace") != "managed-primary" or model is None:
            raise ValueError("Unmapped workspace or model response")
        if model in forbidden:
            raise ValueError(f"Overlapping model observed in {lane} lane: {model}")


def finish_lane(output, *, lane):
    if lane not in ("basic", "smart", "desktop"):
        raise ValueError("Unknown model-ledger lane")
    files = sorted((output / "artifacts").glob("*/model-ledger.json"))
    responses = []
    unmapped = not files
    for path in files:
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
            if case.get("coverage") != "partial":
                unmapped = True
            if isinstance(case.get("responses"), list):
                responses.extend(case["responses"])
            else:
                unmapped = True
        except (OSError, ValueError, AttributeError):
            unmapped = True
    result = {
        "lane": lane,
        "coverage": "unmapped" if unmapped else "partial",
        "timestamp_semantics": "native-response-recorded",
        "case_count": len(files),
        "responses": responses,
    }
    (output / "model-ledger.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if lane in ("smart", "desktop"):
        if unmapped:
            raise ValueError("Model ledger contains unmapped case evidence")
        fixtures = Path(__file__).resolve().parents[2] / "fixtures/managed_config"
        desktop_models, smart_models = model_sets(fixtures)
        check_model_separation(
            responses, lane=lane, desktop_models=desktop_models, smart_models=smart_models
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--lane", required=True, choices=("basic", "smart", "desktop"))
    args = parser.parse_args()
    try:
        finish_lane(args.output, lane=args.lane)
    except ValueError as error:
        raise SystemExit(str(error)) from error
