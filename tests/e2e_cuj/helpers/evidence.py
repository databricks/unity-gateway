"""Read native session evidence; a routing banner is deliberately not an input."""

import json
import re
from dataclasses import asdict, dataclass
from uuid import uuid4


def canonical_model(value):
    """Only documented gateway aliases, not fuzzy family/date matching."""
    value = re.sub(r"^anthropic-aigw-[0-9a-f]{8}-", "", value)
    value = value.removesuffix("[1m]")
    value = value.removeprefix("system.ai.")
    value = re.sub(r"^(gpt-\d+)\.(\d+)", r"\1-\2", value)
    return "system.ai." + value


@dataclass
class FileTask:
    filename: str
    value: str
    prompt: str

    @classmethod
    def create(cls, project):
        filename = f"cuj-{uuid4().hex[:12]}.txt"
        value = uuid4().hex
        (project / filename).write_text(value + "\n")
        return cls(
            filename,
            value,
            f"Read {filename} in this directory and reply with its exact contents. Do not delegate.",
        )


def jsonl(path):
    records = []
    for line in path.read_text().splitlines(keepends=True):
        if not line.endswith("\n"):
            break  # Polling may catch an in-progress final write.
        records.append(json.loads(line))
    return records


def message_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        part.get("text", "") for part in content or [] if part.get("type") in {"text", "input_text"}
    )


@dataclass
class CompletedTurn:
    session_id: str
    turn_id: str
    models: list[str]
    answer: str


def completed_turn(agent, records, task):
    """Match one exact user prompt in one parent session, then its completed answer."""
    if agent == "claude":
        if any(row.get("isSidechain") for row in records):
            return None
        prompts = [
            i
            for i, row in enumerate(records)
            if row.get("type") == "user"
            and message_text(row.get("message", {}).get("content")) == task.prompt
        ]
        if not prompts:
            return None
        assert len(prompts) == 1, "Prompt was submitted more than once"
        first = records[prompts[0]]
        session_id = first.get("sessionId")
        responses = []
        for row in records[prompts[0] + 1 :]:
            msg = row.get("message", {})
            if row.get("type") == "user" and isinstance(msg.get("content"), str):
                break
            if row.get("type") == "assistant":
                assert row.get("sessionId") == session_id and session_id
                responses.append(msg)
        if not responses:
            return None
        last = responses[-1]
        answer = message_text(last.get("content"))
        if (
            task.value not in answer
            or not last.get("id")
            or any(part.get("type") == "tool_use" for part in last.get("content", []))
            or last.get("stop_reason") not in (None, "end_turn", "stop_sequence")
        ):
            return None
        assert all(msg.get("model") for msg in responses), "Missing inference model metadata"
        return CompletedTurn(session_id, last["id"], [msg["model"] for msg in responses], answer)

    meta = [row["payload"] for row in records if row.get("type") == "session_meta"]
    if len(meta) != 1 or isinstance(meta[0].get("source"), dict):
        return None
    contexts, prompt_count, seen_prompt, active_turn = {}, 0, False, None
    for row in records:
        payload = row.get("payload", {})
        if row.get("type") == "turn_context":
            contexts.setdefault(payload.get("turn_id"), []).append(payload.get("model"))
        if row.get("type") != "event_msg":
            continue
        if payload.get("type") == "task_started":
            if seen_prompt:
                return None
            active_turn = payload.get("turn_id")
        if payload.get("type") == "user_message":
            if payload.get("message") == task.prompt:
                prompt_count += 1
                assert prompt_count == 1, "Prompt was submitted more than once"
                seen_prompt = True
            elif seen_prompt:
                return None
        if seen_prompt and payload.get("type") == "task_complete":
            turn_id = payload.get("turn_id")
            answer = payload.get("last_agent_message") or ""
            if (
                task.value in answer
                and turn_id
                and turn_id == active_turn
                and contexts.get(turn_id)
            ):
                assert all(contexts[turn_id]), "Missing native turn model metadata"
                return CompletedTurn(meta[0]["id"], turn_id, contexts[turn_id], answer)
    return None


class SessionEvidence:
    def __init__(self, home, agent):
        self.agent = agent
        self.directory = home / (".claude/projects" if agent == "claude" else ".codex/sessions")
        self.existing = set(self.directory.rglob("*.jsonl"))
        names = [
            "claude-v2-pty.log" if agent == "claude" else "codex-v2-interposer.log",
            f"{agent}-smart-routing-decisions.jsonl",
        ]
        self.boundaries = {
            home / ".ucode" / name: (home / ".ucode" / name).read_bytes()
            if (home / ".ucode" / name).exists()
            else b""
            for name in names
        }

    def new_logs(self):
        result = []
        for path, before in self.boundaries.items():
            after = path.read_bytes() if path.exists() else b""
            # Some launches clear subagent audit artifacts; never accept losing
            # a first-prompt log boundary, or silently re-read stale log lines.
            if path.suffix == ".jsonl" and not after:
                result.append("")
                continue
            assert after.startswith(before), f"Evidence rotated/truncated: {path.name}"
            result.append(after[len(before) :].decode())
        return result

    def completed(self, task):
        found = []
        for path in sorted(set(self.directory.rglob("*.jsonl")) - self.existing):
            if "subagents" in path.parts:
                continue
            turn = completed_turn(self.agent, jsonl(path), task)
            if turn:
                found.append(turn)
        assert len(found) <= 1, "Task matched multiple sessions"
        return found[0] if found else None

    def snapshot(self):
        return {
            "logs": {
                path.name: path.read_text() if path.exists() else "" for path in self.boundaries
            },
            "log_offsets": {path.name: len(before) for path, before in self.boundaries.items()},
            "sessions": {
                str(path.relative_to(self.directory)): jsonl(path)
                for path in sorted(set(self.directory.rglob("*.jsonl")) - self.existing)
            },
        }

    def assert_applied(self, task, supported, *, routed, expected=None):
        turn = self.completed(task)
        assert turn, "No completed native inference correlated to the exact prompt"
        route_log, children = self.new_logs()
        assert not children.strip(), "File task unexpectedly routed a subagent"
        assert supported and all(model.startswith("system.ai.") for model in supported)
        supported = {canonical_model(model) for model in supported}
        if routed:
            pattern = (
                r"\[ROUTE\] first prompt -> '([^']+)'"
                if self.agent == "claude"
                else r"\[ROUTE\] selected '([^']+)'; rationale="
            )
            decisions = re.findall(pattern, route_log)
            assert len(decisions) == 1, "Require one fresh successful router decision, not a banner"
            expected = canonical_model(decisions[0])
            assert expected in supported, (
                f"Router selected a target absent from the live catalog: {expected}"
            )
            if self.agent == "claude":
                assert "[DONE] first prompt confirmed submitted" in route_log
            else:
                requests = re.findall(r"\[ROUTE\] request POST ([^ ]+): (\{.*\})", route_log)
                assert len(requests) == 1, "Missing/duplicate live router request"
                url, body = requests[0]
                request = json.loads(body)
                assert url.endswith("/ai-gateway/routing/v1/routes:select")
                assert request["task"]["prompt"] == task.prompt
                assert request["route_selector"]["router_name"]
                options = request["route_options"]
                assert all(option["harness"] == "codex" for option in options)
                assert expected in {canonical_model(option["model"]) for option in options}
        else:
            assert not route_log.strip(), "Bypassed/disabled session emitted new routing activity"
            assert expected is not None
            expected = canonical_model(expected)
            assert expected in supported
        assert {canonical_model(model) for model in turn.models} == {expected}, (
            f"Inference models {turn.models} do not match selected model {expected}"
        )
        return {
            **asdict(turn),
            "prompt": task.prompt,
            "selected_model": expected,
            "routing_log": route_log,
            "routed": routed,
        }
