"""Read native session evidence for completed agent turns."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

import httpx

from tests.integration.utils.evidence import FileTask, read_jsonl
from tests.integration.utils.model_discovery import claude_discovery_model_id
from tests.integration.utils.provider_catalog import MODEL_SERVICE_PARENT_SCHEMA_HEADER

from .constants import CLAUDE, CODEX, INFERENCE_PATHS, NATIVE_MODEL_ALIASES


def canonical_model(value):
    """Only documented gateway aliases, not fuzzy family/date matching."""
    # Claude reports a session-scoped gateway prefix, for example
    # `anthropic-aigw-1234abcd-system.ai.glm-5-2`.
    value = re.sub(r"^anthropic-aigw-[0-9a-f]{8}-", "", value)
    value = value.removesuffix("[1m]")
    value = value.removeprefix("system.ai.")
    # Codex writes dotted major/minor versions (`gpt-5.6-sol`) while the
    # gateway model identifier uses a hyphen (`system.ai.gpt-5-6-sol`).
    value = re.sub(r"^(gpt-\d+)\.(\d+)", r"\1-\2", value)
    return "system.ai." + value


def assert_models(models, expected):
    """Every canonical model equals `expected`; not for UC models, which canonical_model mangles."""
    actual = {NATIVE_MODEL_ALIASES.get(model, model) for model in map(canonical_model, models)}
    assert actual == {canonical_model(expected)}, (models, expected)


def assert_served(recorder, request, model):
    """The request asked for `model` and its paired response was a non-empty HTTP 200."""
    assert request.payload["model"] == model, request.payload
    response = recorder.response_for(request, timeout=240)
    assert response.status_code == 200, {
        "status": response.status_code,
        "model": request.payload["model"],
        "output_config": request.payload.get("output_config"),
        "thinking": request.payload.get("thinking"),
        "response_body": httpx.Response(
            response.status_code, headers=response.headers, content=response.body
        ).text[:2000],
    }
    assert response.body, "Inference response was empty"


def served_inference_request(recorder, requests, request, agent):
    """Require HTTP 200 or verified native Claude compatibility retries."""
    model = request.payload["model"]
    # Claude 2.1.290 may remove display, then safeguards after separate Bedrock
    # validation errors. Inspect recorded traffic only; never replay or edit it.
    # Each field may disappear once, and the full task/model/budget/effort must survive.
    for _ in range(3):
        payload = request.payload
        thinking = payload.get("thinking", {})
        thinking_type = thinking.get("type")
        response = recorder.response_for(request, timeout=240)
        error_message = None
        if agent == CLAUDE and response.status_code == 400:
            try:
                error = httpx.Response(
                    response.status_code, headers=response.headers, content=response.body
                ).json()
                if isinstance(error, dict) and error.get("error_code") == "BAD_REQUEST":
                    detail = json.loads(error.get("message", ""))
                    if isinstance(detail, dict) and set(detail) == {"message"}:
                        error_message = detail["message"]
            except (ValueError, TypeError):
                pass
        expected_retry = None
        if (
            thinking_type in {"adaptive", "enabled"}
            and thinking.get("display") == "updates"
            and error_message
            == f"thinking.{thinking_type}.display: Input should be 'summarized', 'omitted'"
        ):
            expected_retry = {
                **payload,
                "thinking": {key: value for key, value in thinking.items() if key != "display"},
            }
        elif (
            "safeguards" in payload
            and error_message == "safeguards: Extra inputs are not permitted"
        ):
            expected_retry = {key: value for key, value in payload.items() if key != "safeguards"}
        if expected_retry is None:
            assert_served(recorder, request, model)
            return request
        following = requests[requests.index(request) + 1 :]
        # Parent and child traffic can interleave on the same endpoint.
        retry = next(
            (
                candidate
                for candidate in following
                if candidate.method == request.method
                and candidate.path == request.path
                and candidate.payload.get("system") == payload.get("system")
                and candidate.payload.get("metadata") == payload.get("metadata")
            ),
            None,
        )
        assert retry is not None, "Claude compatibility rejection had no retry"
        assert retry.payload == expected_retry, (
            "Claude compatibility retry changed more than the rejected field",
            error_message,
            retry.payload,
        )
        request = retry
    raise AssertionError("Claude compatibility retries did not reach a successful response")


def assert_claude_headless_model(result, expected):
    final = None
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "result":
            final = payload
    assert final is not None and not final.get("is_error"), result.stdout
    usage = final["modelUsage"]
    assert set(usage) == {expected}, {"expected": expected, "observed": sorted(usage)}
    assert usage[expected]["outputTokens"] > 0, usage


def _request_contains_task(request, agent, task):
    field = "messages" if agent == CLAUDE else "input"
    entries = request.payload.get(field)
    if not isinstance(entries, (list, str)):
        return False
    if isinstance(entries, str):
        return entries == task.prompt
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        content = entry.get("content", "")
        if isinstance(content, str) and content == task.prompt:
            return True
        if isinstance(content, list) and any(
            message_text([part]) == task.prompt for part in content if isinstance(part, dict)
        ):
            return True
    return False


def assert_inference_evidence(recorder, checkpoint, agent, task, expected, *, parent_schema=None):
    expected_wire_model = claude_discovery_model_id(expected) if agent == CLAUDE else expected
    requests = recorder.requests_after(checkpoint)
    inference_requests = [
        request
        for request in requests
        if request.method == "POST" and request.path == INFERENCE_PATHS[agent]
    ]
    assert inference_requests, {
        "agent": agent,
        "path": INFERENCE_PATHS[agent],
        "requests": [(request.method, request.path) for request in requests],
    }
    task_requests = [
        request for request in inference_requests if _request_contains_task(request, agent, task)
    ]
    assert task_requests, "No inference request contained the submitted task prompt"
    for request in task_requests:
        if parent_schema is not None:
            assert (
                request.headers.get(MODEL_SERVICE_PARENT_SCHEMA_HEADER.lower()) == parent_schema
            ), request.headers
        served = served_inference_request(recorder, task_requests, request, agent)
        assert_served(recorder, served, expected_wire_model)


def claude_file_task(session):
    """A FileTask naming its absolute path, so Claude reads it without a `find` permission prompt."""
    task = FileTask(session)
    task.prompt = (
        f"Use the Read tool to read {session.cwd / task.filename}. Reply with only its contents."
    )
    return task


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


@dataclass
class SessionObservation:
    """Stable native evidence captured before the next TUI session starts."""

    helper: type[BaseCujHelper]
    turn: CompletedTurn | None

    def assert_applied(self, task, supported, *, expected):
        assert self.turn, "No completed native inference correlated to the exact prompt"
        return self.helper.assert_applied(self.turn, task, supported, expected=expected)


class BaseCujHelper(ABC):
    session_directory: str

    @staticmethod
    @abstractmethod
    def _completed_turn(records, task):
        """Match the exact prompt to a completed native parent turn."""
        raise NotImplementedError

    @classmethod
    def assert_applied(cls, turn, task, supported, *, expected):
        assert supported and all(model.startswith("system.ai.") for model in supported)
        supported = {canonical_model(model) for model in supported}
        expected = canonical_model(expected)
        assert expected in supported
        return {
            **asdict(turn),
            "prompt": task.prompt,
            "selected_model": expected,
        }


class ClaudeCujHelper(BaseCujHelper):
    session_directory = ".claude/projects"

    @staticmethod
    def _completed_turn(records, task):
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


class CodexCujHelper(BaseCujHelper):
    session_directory = ".codex/sessions"

    @staticmethod
    def _completed_turn(records, task):
        meta = [row["payload"] for row in records if row.get("type") == "session_meta"]
        if len(meta) != 1 or isinstance(meta[0].get("source"), dict):
            return None
        contexts, active_turn, target_turn = {}, None, None
        started_turn, aborted_turn, answer, prompt_sources = None, None, None, set()
        for row in records:
            payload = row.get("payload", {})
            metadata = row.get("internal_chat_message_metadata_passthrough") or {}
            if "multi_agent.subagent_notification" in metadata.get(
                "content_item_kinds", ()
            ) or "<subagent_notification>" in str(
                payload.get("content") or payload.get("message")
            ).replace("-", "_"):
                continue
            row_turn = payload.get("turn_id")
            if row.get("type") == "turn_context":
                contexts.setdefault(row_turn, []).append(payload.get("model"))
                if target_turn and row_turn != target_turn:
                    return None
                active_turn = row_turn
                continue
            source = None
            if row.get("type") == "event_msg" and payload.get("type") == "user_message":
                source, prompt = "event", payload.get("message")
            elif (
                row.get("type") == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                source, prompt = "response", message_text(payload.get("content"))
            if source:
                if prompt == task.prompt:
                    assert source not in prompt_sources, "Prompt was submitted more than once"
                    prompt_sources.add(source)
                    if target_turn and row_turn and row_turn != target_turn:
                        return None
                    target_turn = target_turn or row_turn or active_turn
                elif prompt_sources:
                    return None
                continue
            if row.get("type") != "event_msg":
                continue
            event_type = payload.get("type")
            if event_type == "task_started":
                turn_id = payload.get("turn_id")
                if not turn_id or turn_id == aborted_turn:
                    return None
                if target_turn and turn_id != target_turn:
                    return None
                active_turn = started_turn = turn_id
                if prompt_sources and target_turn is None:
                    target_turn = turn_id
            elif event_type == "task_complete":
                turn_id = payload.get("turn_id")
                if target_turn:
                    if turn_id != target_turn:
                        return None
                    answer = payload.get("last_agent_message") or ""
                    break
                if turn_id == active_turn:
                    active_turn = started_turn = None
            elif event_type == "turn_aborted":
                aborted_turn = payload.get("turn_id")
                if aborted_turn == target_turn:
                    return None
                if aborted_turn == active_turn:
                    active_turn = started_turn = None
        if not prompt_sources or not target_turn or started_turn != target_turn or answer is None:
            return None
        models = contexts.get(target_turn)
        if task.value not in answer or not models:
            return None
        assert all(models), "Missing native turn model metadata"
        return CompletedTurn(meta[0]["id"], target_turn, models, answer)


def get_cuj_helper(agent):
    if agent == CLAUDE:
        return ClaudeCujHelper
    elif agent == CODEX:
        return CodexCujHelper
    else:
        raise ValueError(f"Unsupported agent: {agent!r}")


def completed_turn(agent, records, task):
    return get_cuj_helper(agent)._completed_turn(records, task)


class SessionEvidence:
    def __init__(self, home, agent):
        self.helper = get_cuj_helper(agent)
        self.directory = home / self.helper.session_directory
        self.existing = set(self.directory.rglob("*.jsonl"))

    def completed(self, task):
        found = []
        for path in sorted(set(self.directory.rglob("*.jsonl")) - self.existing):
            if "subagents" in path.parts:
                continue
            turn = self.helper._completed_turn(read_jsonl(path), task)
            if turn:
                found.append(turn)
        assert len(found) <= 1, "Task matched multiple sessions"
        return found[0] if found else None

    def assert_models(self, task, expected):
        turn = self.completed(task)
        assert turn is not None, f"No completed native turn matched {task.prompt!r}"
        assert_models(turn.models, expected)

    def observe(self, task):
        return SessionObservation(helper=self.helper, turn=self.completed(task))

    def assert_applied(self, task, supported, *, expected):
        return self.observe(task).assert_applied(task, supported, expected=expected)
