"""Read native session evidence for completed agent turns."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from tests.integration.utils.agents import CompletedTurn, codex, message_text
from tests.integration.utils.evidence import FileTask, read_jsonl
from tests.integration.utils.model_discovery import claude_discovery_model_id
from tests.integration.utils.provider_catalog import MODEL_SERVICE_PARENT_SCHEMA_HEADER
from tests.integration.utils.request_recorder import (
    assert_served as assert_served,
)
from tests.integration.utils.request_recorder import (
    served_inference_request as served_inference_request,
)

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


def _answer_matches(task, answer):
    """task.value=None accepts any nonempty final answer."""
    return bool(answer.strip()) and (task.value is None or task.value in answer)


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
            isinstance(task, FileTask)
            and task.value is not None
            and task.prompt != task.delegate_prompt
            and last.get("stop_reason") in ("end_turn", "stop_sequence")
            and not any(part.get("type") == "tool_use" for part in last.get("content", []))
        ):
            assert task.value in answer, (
                f"Claude completed the file task with an incorrect answer: {answer!r}; "
                f"expected file value {task.value!r}"
            )
        if (
            not _answer_matches(task, answer)
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
        turn = codex.completed_prompt_turn(records, task.prompt)
        if turn is None:
            return None
        if (
            isinstance(task, FileTask)
            and task.value is not None
            and task.prompt != task.delegate_prompt
        ):
            assert task.value in turn.answer, (
                f"Codex completed the file task with an incorrect answer: {turn.answer!r}; "
                f"expected file value {task.value!r}"
            )
        return turn if _answer_matches(task, turn.answer) else None


def get_cuj_helper(agent):
    if agent == CLAUDE:
        return ClaudeCujHelper
    elif agent == CODEX:
        return CodexCujHelper
    else:
        raise ValueError(f"Unsupported agent: {agent!r}")


def completed_turn(agent, records, task):
    """Match a completed turn; task.value=None accepts any nonempty final answer."""
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
