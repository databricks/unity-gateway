"""VS Code chat evidence, read from the session events Copilot Chat's Copilot session saves."""

import time

from ..evidence import agent_sessions

__all__ = ["chat_turns"]

SAVE_TIMEOUT_S = 60


def _turns(records: list[dict]) -> dict[str, dict]:
    """Each prompt of one session: the model it ran on, its tools, error and answer text."""
    turns, turn, model, final = {}, None, None, False
    for record in records:
        kind, data = record.get("type"), record.get("data") or {}
        if kind == "session.start":
            model = data.get("selectedModel")
        elif kind == "session.model_change":
            model = data.get("newModel")
        elif kind == "user.message":
            prompt = (data.get("content") or "").strip()
            turn = turns[prompt] = {
                "model": model,
                "prompt": prompt,
                "tools": [],
                "error": None,
                "text": "",
                "done": False,
            }
            final = False
        elif turn is None:
            continue
        elif kind == "tool.execution_start":
            turn["tools"].append(data.get("toolName"))
        elif kind == "session.error":
            turn["error"], turn["done"] = data, True
        elif kind == "assistant.message":
            turn["text"] += data.get("content") or ""
            final = not data.get("toolRequests")
        elif kind == "assistant.turn_end":
            # Every tool round ends a turn too; only the round that asks for no tools ends the prompt.
            turn["done"] = turn["done"] or final
    return turns


def chat_turns(session, prompts: list[str]) -> list[dict]:
    """Wait until the Copilot session has finished each prompt; return the turns in order.

    Turns are matched by prompt, not by session: a reopened window starts a new session."""
    wanted = [prompt.strip() for prompt in prompts]
    deadline = time.monotonic() + SAVE_TIMEOUT_S
    while time.monotonic() < deadline:
        found = {}
        for records in agent_sessions(session, "copilot").values():
            for prompt, turn in _turns(records).items():
                if turn.pop("done") and prompt in wanted:
                    found[prompt] = turn
        if len(found) == len(wanted):
            turns = [found[prompt] for prompt in wanted]
            session.record("vscode-chat-session.json", turns)
            return turns
        time.sleep(1)
    raise AssertionError(f"The Copilot session did not finish every prompt in {SAVE_TIMEOUT_S}s")
