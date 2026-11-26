"""Extract redacted, explicitly partial model usage from native agent sessions."""

import argparse
import json
import os
import re
from datetime import UTC, datetime
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


def reported_model(model):
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.\-\[\]]{1,160}", model):
        return model
    return None


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
    if workspace not in (None, "managed-primary", "staging"):
        raise ValueError("Unknown traffic-ledger workspace label")
    responses = []
    seen = {}
    unmapped = workspace is None
    model_unmapped = False
    for path, records in codex_sessions.items():
        contexts = {}
        reported_contexts = {}
        for record in records:
            if not isinstance(record, dict) or record.get("type") != "turn_context":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                continue
            raw = reported_model(payload.get("model"))
            model = canonical_model(raw, known_models)
            if turn_id in contexts and (contexts[turn_id] or reported_contexts[turn_id]) != (
                model or raw
            ):
                unmapped = model_unmapped = True
            contexts[turn_id] = model
            reported_contexts[turn_id] = raw
        for record in records:
            if not isinstance(record, dict) or record.get("type") != "token_usage_record":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                unmapped = model_unmapped = True
                continue
            identifier = payload.get("response_id")
            if not isinstance(identifier, str) or not identifier:
                unmapped = model_unmapped = True
                continue
            key = ("codex", payload.get("session_id") or path, identifier)
            turn_id = payload.get("turn_id")
            model = contexts.get(turn_id)
            raw = reported_contexts.get(turn_id)
            if key in seen:
                if seen[key] != (model or raw):
                    unmapped = model_unmapped = True
                continue
            seen[key] = model or raw
            model_unmapped |= raw is None
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
                    "reported_model": raw,
                    "workspace": workspace,
                    "usage": usage,
                }
            )
    claude_responses = {}
    for path, records in claude_sessions.items():
        for record in records:
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                unmapped = model_unmapped = True
                continue
            identifier = message.get("id")
            if not isinstance(identifier, str) or not identifier:
                unmapped = model_unmapped = True
                continue
            key = ("claude", record.get("sessionId") or path, identifier)
            raw = reported_model(message.get("model"))
            model = canonical_model(raw, known_models)
            timestamp = _timestamp(record.get("timestamp"))
            usage, complete_usage = _usage(message.get("usage"))
            unmapped |= (
                model is None or timestamp is None or not complete_usage or workspace is None
            )
            model_unmapped |= raw is None
            if key in seen:
                if seen[key] != (model or raw):
                    unmapped = model_unmapped = True
                previous = claude_responses[key]
                for field, value in usage.items():
                    previous["usage"][field] = max(previous["usage"].get(field, 0), value)
                if timestamp and (
                    previous["timestamp"] is None
                    or datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    > datetime.fromisoformat(previous["timestamp"].replace("Z", "+00:00"))
                ):
                    previous["timestamp"] = timestamp
                continue
            seen[key] = model or raw
            response = {
                "source": "claude-native",
                "timestamp": timestamp,
                "model": model,
                "reported_model": raw,
                "workspace": workspace,
                "usage": usage,
            }
            claude_responses[key] = response
            responses.append(response)
    responses.sort(key=lambda response: response["timestamp"] or "")
    return {
        "coverage": "unmapped" if unmapped else "partial",
        "model_coverage": "unmapped" if model_unmapped else "partial",
        "timestamp_semantics": "native-response-recorded",
        "responses": responses,
    }


def staging_desktop_usage(sessions_root, *, since, model):
    started = datetime.fromisoformat(since.replace("Z", "+00:00"))
    sessions = {}
    incomplete = not sessions_root.is_dir()
    for path in sessions_root.glob("**/projects/session/*.jsonl"):
        try:
            if path.stat().st_mtime < started.timestamp():
                continue
            records = []
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    incomplete = True
                    continue
                timestamp = _timestamp(row.get("timestamp")) if isinstance(row, dict) else None
                if (
                    timestamp is None
                    or datetime.fromisoformat(timestamp.replace("Z", "+00:00")) >= started
                ):
                    records.append(row)
            sessions[str(path)] = records
        except (OSError, UnicodeError):
            incomplete = True
    result = native_usage({}, sessions, workspace="staging", known_models={model})
    result["lane"] = "staging-desktop"
    if incomplete or not result["responses"]:
        result["coverage"] = result["model_coverage"] = "unmapped"
    return result


def record_case_usage(session, *, workspace):
    from .evidence import agent_sessions

    fixtures = Path(__file__).resolve().parents[2] / "fixtures/managed_config"
    desktop_models, smart_models = model_sets(fixtures)
    known_models = desktop_models | smart_models
    if model := os.environ.get("UG_INTEGRATION_CLAUDE_PROVIDER_MODEL"):
        known_models.add(model)
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
    if lane not in ("desktop", "smart", "basic-claude", "basic-codex"):
        raise ValueError("Unknown model-guard lane")
    if not responses and lane in ("desktop", "smart"):
        raise ValueError("No observed model responses")
    known_models = desktop_models | smart_models
    for response in responses:
        if lane.startswith("basic-"):
            raw = reported_model(response.get("reported_model") or response.get("model"))
            if raw is None:
                raise ValueError("Unmapped basic-lane model response")
            bare = raw.removeprefix("system.ai.").split("[", 1)[0]
            mapped = response.get("model")
            if mapped and mapped.removeprefix("system.ai.") != bare:
                raise ValueError("Conflicting basic-lane model attribution")
            model = canonical_model(bare, known_models)
            if (
                model in known_models
                or (lane == "basic-claude" and bare.startswith("gpt-"))
                or (lane == "basic-codex" and not bare.startswith("gpt-"))
            ):
                raise ValueError(f"Overlapping model observed in {lane} lane: {raw}")
        else:
            model = canonical_model(response.get("model"), known_models)
            if response.get("workspace") != "managed-primary" or model is None:
                raise ValueError("Unmapped workspace or model response")
            forbidden = desktop_models if lane == "smart" else smart_models
            if model in forbidden:
                raise ValueError(f"Overlapping model observed in {lane} lane: {model}")


def finish_lane(output, *, lane):
    if lane not in ("basic-claude", "basic-codex", "smart", "desktop", "staging"):
        raise ValueError("Unknown model-ledger lane")
    files = sorted((output / "artifacts").glob("*/model-ledger.json"))
    responses = []
    unmapped = not files
    model_unmapped = not files
    for path in files:
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
            if case.get("coverage") != "partial":
                unmapped = True
            if case.get("model_coverage") != "partial":
                model_unmapped = True
            if isinstance(case.get("responses"), list):
                responses.extend(case["responses"])
            else:
                unmapped = model_unmapped = True
        except (OSError, ValueError, AttributeError):
            unmapped = model_unmapped = True
    result = {
        "lane": lane,
        "coverage": "unmapped" if unmapped else "partial",
        "model_coverage": "unmapped" if model_unmapped else "partial",
        "timestamp_semantics": "native-response-recorded",
        "case_count": len(files),
        "responses": responses,
    }
    (output / "model-ledger.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if lane == "staging":
        return result
    if model_unmapped or (lane in ("smart", "desktop") and unmapped):
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
    parser.add_argument(
        "--lane",
        required=True,
        choices=("basic-claude", "basic-codex", "smart", "desktop", "staging", "staging-desktop"),
    )
    parser.add_argument("--sessions-root", type=Path)
    parser.add_argument("--since-file", type=Path)
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.lane == "staging-desktop":
        if not (args.sessions_root and args.since_file and args.model):
            parser.error("staging-desktop requires --sessions-root, --since-file and --model")
        try:
            since = datetime.fromtimestamp(args.since_file.stat().st_mtime, UTC).isoformat()
        except OSError as error:
            result = {
                "lane": "staging-desktop",
                "coverage": "unmapped",
                "model_coverage": "unmapped",
                "timestamp_semantics": "native-response-recorded",
                "responses": [],
                "error": type(error).__name__,
            }
        else:
            result = staging_desktop_usage(args.sessions_root, since=since, model=args.model)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "model-ledger.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
    else:
        try:
            finish_lane(args.output, lane=args.lane)
        except ValueError as error:
            raise SystemExit(str(error)) from error
