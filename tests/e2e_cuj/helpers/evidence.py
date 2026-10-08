"""Read native session evidence for completed agent turns."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from tests.integration.utils.evidence import FileTask, read_jsonl

from .constants import CLAUDE, CODEX, NATIVE_MODEL_ALIASES


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
        "response_body": response.body.decode("utf-8", errors="replace")[:2000],
    }
    assert response.body, "Inference response was empty"


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
        contexts, prompt_count, seen_prompt, active_turn = {}, 0, False, None
        for row in records:
            payload = row.get("payload", {})
            if row.get("type") == "turn_context":
                contexts.setdefault(payload.get("turn_id"), []).append(payload.get("model"))
            if (
                row.get("type") == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                prompt = message_text(payload.get("content"))
                if prompt == task.prompt:
                    prompt_count += 1
                    assert prompt_count == 1, "Prompt was submitted more than once"
                    seen_prompt = True
                elif seen_prompt:
                    return None
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
