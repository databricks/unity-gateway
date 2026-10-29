"""Read real agent transcripts and ug routing records without changing them."""

import json
import re
import uuid
from pathlib import Path

from .agents import _evidence_id, antigravity, claude, codex, copilot, grok

_AGENT_HELPERS = {"claude": claude, "codex": codex, "copilot": copilot, "grok": grok}


def assert_no_terminal_api_error(screen: str) -> None:
    """Fail on definitive client errors, not an in-progress transient retry."""
    error = re.search(
        r"unexpected status (?:400|401|403|404|405|409|422)\b|PERMISSION_DENIED"
        r"|exceeded retry limit",
        screen,
        re.IGNORECASE,
    )
    assert error is None, "Agent returned a terminal API error:\n" + screen


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    records = []
    for index, line in enumerate(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            # A running agent may not have finished its last write yet.
            if index == len(lines) - 1 and not line.endswith("\n"):
                break
            raise
        if isinstance(value, dict):
            records.append(value)
    return records


def agent_sessions(session, agent: str) -> dict[str, list[dict]]:
    directory = session.home / _AGENT_HELPERS.get(agent, codex).SESSION_DIRECTORY
    return {
        path.relative_to(directory).as_posix(): read_jsonl(path)
        for path in directory.rglob("*.jsonl")
    }


def assistant_answers(agent: str, records: list[dict]) -> list[str]:
    helper = _AGENT_HELPERS.get(agent)
    return helper.assistant_answers(records) if helper is not None else []


def tool_outputs(agent: str, records: list[dict]) -> list[str]:
    """Read native tool results even when their terminal output is collapsed."""
    contents = []
    for record in records:
        if agent == "claude" and record.get("type") == "user":
            contents.extend(
                part.get("content")
                for part in record.get("message", {}).get("content", [])
                if isinstance(part, dict)
                and part.get("type") == "tool_result"
                and not part.get("is_error")
            )
        if agent == "codex" and record.get("type") == "response_item":
            payload = record.get("payload", {})
            if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                contents.append(payload.get("output"))

    outputs = []
    for content in contents:
        if isinstance(content, str):
            outputs.append(content)
        elif isinstance(content, list):
            outputs.extend(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") in {"text", "input_text"}
                and isinstance(part.get("text"), str)
            )
    return outputs


def is_child_session(agent: str, path: str, records: list[dict]) -> bool:
    return _AGENT_HELPERS.get(agent, codex).is_child_session(path, records)


def completed_task_models(session, agent: str, answer_value: str) -> set[str]:
    """Read parent task model evidence, not proof of the gateway's destination."""
    assert agent in _AGENT_HELPERS, f"Unsupported evidence agent: {agent}"
    adapter = _AGENT_HELPERS[agent]
    assert isinstance(answer_value, str) and answer_value.strip(), (
        "Expected a nonempty answer value"
    )
    sessions = agent_sessions(session, agent)
    session.record("agent-sessions.json", sessions)
    return {
        model
        for path, records in sessions.items()
        if not is_child_session(agent, path, records)
        for model in adapter.completed_task_models(records, answer_value)
    }


def assert_completed_task_model(session, agent: str, answer_value: str, expected: str) -> None:
    expected = _evidence_id(expected, "expected model")
    observed = completed_task_models(session, agent, answer_value)
    adapter = _AGENT_HELPERS[agent]
    session.record(
        f"completed-task-model-{agent}-{answer_value[:12]}.json",
        {
            "expected": expected,
            "observed": sorted(observed),
            "evidence_kind": adapter.EVIDENCE_KIND,
            "gateway_destination_proven": False,
        },
    )
    assert observed == {expected}, {"expected": expected, "observed": sorted(observed)}


def assistant_answer_contains(session, agent: str, value: str, *, child: bool = False) -> bool:
    """Whether a native parent or child assistant answer contains ``value``."""
    return any(
        value in answer
        for path, records in agent_sessions(session, agent).items()
        if is_child_session(agent, path, records) == child
        for answer in assistant_answers(agent, records)
    )


def assert_no_bearer_on_disk(session) -> None:
    """No file under the session home holds the Databricks bearer."""
    bearer = session.env.get("DATABRICKS_BEARER")
    assert bearer, "The live suite authenticates with DATABRICKS_BEARER."
    encoded = bearer.encode()
    for path in session.home.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert encoded not in path.read_bytes(), f"{path} holds the Databricks credential"
    env_file = session.home / ".copilot/ucode.env"
    if env_file.exists():
        text = env_file.read_text(encoding="utf-8")
        assert "COPILOT_PROVIDER_BEARER_TOKEN" not in text
        assert "OAUTH_TOKEN" not in text


def copilot_selected_models(session) -> list[str]:
    """Every registry ref Copilot sessions under this home started on or switched to."""
    return [
        ref
        for records in agent_sessions(session, "copilot").values()
        for ref in copilot.selected_models(records)
    ]


def assert_no_session_registry_left(session) -> None:
    """`ug copilot` removes its per-session registry, which holds the proxy secret, on exit."""
    leftovers = sorted(session.home.glob(".copilot/ug-providers-*"))
    assert not leftovers, f"Session registries left behind: {leftovers}"


def fresh_id():
    """Return a 12-hex-char id; models drop characters when copying longer ones."""
    return uuid.uuid4().hex[:12]


def fresh_session_id():
    """Return a full UUID for Claude's `--session-id`, which rejects shorter ids."""
    return str(uuid.uuid4())


class FileTask:
    """Ordinary project input; the expected answer is never included in the prompt."""

    def __init__(self, session):
        self.value = fresh_id()
        self.filename = "input-" + fresh_id() + ".txt"
        (session.cwd / self.filename).write_text(self.value + "\n")
        # Say "local file" so agents with MCP fixture tools (read_fixture) don't pick those.
        self.prompt = (
            f"Read the local file {self.filename} in the current directory using a tool. "
            "Reply with only its contents."
        )
        # Small models can drop path segments after a space when rebuilding the absolute path.
        self.prompt_with_path = (
            f"Read the local file {session.cwd / self.filename} using a tool. "
            "Reply with only its contents."
        )
        self.delegate_prompt = (
            f"Delegate this task to one subagent: read the local file {self.filename} in the "
            "current directory using a tool and return its contents. Do not read the file "
            "yourself. Wait for the subagent and reply with only the value it returned."
        )

    def completed(self, session, agent: str, *, child: bool = False) -> bool:
        if agent == "antigravity":
            # Antigravity keeps conversations in SQLite, not JSONL, and has no child sessions.
            return not child and antigravity.answer_contains(session, self.value)
        return assistant_answer_contains(session, agent, self.value, child=child)

    def assert_completed(self, session, agent: str, *, child: bool = False) -> None:
        if agent != "antigravity":
            sessions = agent_sessions(session, agent)
            session.record("agent-sessions.json", sessions)
        assert self.completed(session, agent, child=child), (
            f"No {'child' if child else 'parent'} assistant answer contained the file's value; "
            "echoed prompts and tool results do not count as completed answers."
        )

    def assert_headless_answer(self, agent: str, result) -> None:
        """Read the real CLI's structured final answer, never its echoed input."""
        payloads = []
        for line in result.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue  # ug may print human-readable launch status before agent JSON.
            if isinstance(value, dict):
                payloads.append(value)
        if agent == "claude":
            final = [row for row in payloads if row.get("type") == "result"]
            assert final and not final[-1].get("is_error"), result.stdout
            assert self.value in final[-1].get("result", ""), result.stdout
        elif agent == "codex":
            assert any(row.get("type") == "turn.completed" for row in payloads), result.stdout
            answers = [
                row.get("item", {}).get("text", "")
                for row in payloads
                if row.get("type") == "item.completed"
                and row.get("item", {}).get("type") == "agent_message"
            ]
            assert any(self.value in answer for answer in answers), result.stdout
        elif agent == "opencode":
            assert result.returncode == 0, result.stdout
            reads = [
                (row.get("part") or {})
                for row in payloads
                if row.get("type") == "tool_use"
                and (row.get("part") or {}).get("tool") == "read"
                and ((row.get("part") or {}).get("state") or {}).get("status") == "completed"
            ]
            # The Read must target the fixture file, not some other path.
            assert any(
                self.filename in json.dumps((part.get("state") or {}).get("input", {}))
                for part in reads
            ), result.stdout
            # Assert the value in the assistant's text, not the tool output that echoes the file.
            answer = "".join(
                (row.get("part") or {}).get("text", "")
                for row in payloads
                if row.get("type") == "text"
            )
            assert self.value in answer, result.stdout
        elif agent == "antigravity":
            assert result.returncode == 0, result.stdout
            events = antigravity.stream_events(result.stdout)
            # The read must target the fixture file, not some other path.
            reads = antigravity.completed_tools(events, "read_file")
            assert any(
                str((read.get("parameters") or {}).get("Path", "")).endswith(self.filename)
                for read in reads
            ), result.stdout
            # The final answer, not the tool output that echoes the file.
            assert self.value in antigravity.final_response(events), result.stdout
        elif agent == "copilot":
            assert result.returncode == 0, result.stdout
            assert any(row.get("type") == "assistant.turn_end" for row in payloads), result.stdout
            answers = [
                (row.get("data") or {}).get("content", "")
                for row in payloads
                if row.get("type") == "assistant.message"
            ]
            assert any(self.value in answer for answer in answers), result.stdout
        elif agent == "grok":
            assert result.returncode == 0, result.stdout
            records = grok.events(result.stdout)
            assert grok.end_event(records).get("stopReason") == "end_turn", result.stdout
            # The Read must target the fixture file, not some other path.
            assert any(
                self.filename in json.dumps(read) for read in grok.completed_reads(records)
            ), result.stdout
            assert self.value in grok.answer(records), result.stdout
        else:
            raise AssertionError(f"Unknown agent: {agent}")


class SubagentCalculation:
    """A uniquely tagged calculation that must be delegated to a real child."""

    def __init__(self, expression: str, expected: str):
        # Codex exposes task_name, rather than the child message, to its routing hook.
        # Keep the correlation marker valid as a native task name for both agents.
        self.marker = "ug_subagent_" + fresh_id()
        self.value = f"{self.marker}={expected}"
        self.prompt = (
            f'Please spawn exactly one subagent with task name "{self.marker}" '
            f"to calculate {expression}. "
            f'Tell the subagent to reply exactly "{self.value}". '
            "Do not calculate it yourself. Wait for the subagent and then reply with its result."
        )

    def completed(self, session, agent: str, *, child: bool = False) -> bool:
        return assistant_answer_contains(session, agent, self.value, child=child)

    def assert_completed(self, session, agent: str, *, child: bool = False) -> None:
        sessions = agent_sessions(session, agent)
        session.record(f"agent-sessions-{self.marker}.json", sessions)
        assert self.completed(session, agent, child=child), (
            f"No {'child' if child else 'parent'} assistant answer contained {self.value!r}; "
            "echoed prompts and tool inputs do not count as completed answers."
        )


def assert_subagent_routed(
    session,
    agent: str,
    task: FileTask | SubagentCalculation,
    *,
    decision_ids: set[str] | None = None,
) -> None:
    """Require a real gateway decision correlated with an actual child start."""
    root = session.home / ".ucode"
    decisions = read_jsonl(root / f"{agent}-smart-routing-decisions.jsonl")
    if decision_ids is not None:
        decisions = [row for row in decisions if row.get("decision_id") in decision_ids]
    audit = read_jsonl(root / f"{agent}-smart-routing-audit.jsonl")
    artifact = "subagent-routing"
    if isinstance(task, SubagentCalculation):
        artifact += f"-{task.marker}"
    session.record(f"{artifact}.json", {"decisions": decisions, "starts": audit})
    assert decisions, "No real subagent routing decision was recorded"
    for decision in decisions:
        assert decision.get("requested_model") and decision.get("router_model"), decision
    if agent == "codex":
        # Codex exposes parent linkage and the child's actual turn model in its
        # native rollouts. Its ug SubagentStart audit can be empty even when the
        # child ran. Match native evidence, including the completed file task.
        sessions = agent_sessions(session, agent)
        linked = []
        for path, records in sessions.items():
            metadata = next(
                (row["payload"] for row in records if row.get("type") == "session_meta"), {}
            )
            source = metadata.get("source")
            if not isinstance(source, dict):
                continue
            parent_id = source.get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
            if not parent_id:
                continue
            # A child rollout starts with inherited parent history. Exclude
            # those turn IDs so a parent's answer/model cannot satisfy this check.
            parent_turn_ids = set()
            for other in sessions.values():
                first_meta = next(
                    (row["payload"] for row in other if row.get("type") == "session_meta"), {}
                )
                if first_meta.get("id") == parent_id:
                    parent_turn_ids.update(
                        row["payload"]["turn_id"]
                        for row in other
                        if row.get("type") == "turn_context" and row["payload"].get("turn_id")
                    )
            assert parent_turn_ids, f"No native parent turns found for {parent_id}"
            for decision in decisions:
                if decision.get("session_id") != parent_id:
                    continue
                routed_turn_ids = {
                    row["payload"]["turn_id"]
                    for row in records
                    if row.get("type") == "turn_context"
                    and row["payload"].get("model") == decision["requested_model"]
                    and row["payload"].get("turn_id")
                } - parent_turn_ids
                for row in records:
                    payload = row.get("payload", {})
                    if (
                        row.get("type") == "event_msg"
                        and payload.get("type") == "task_complete"
                        and payload.get("turn_id") in routed_turn_ids
                        and task.value in (payload.get("last_agent_message") or "")
                    ):
                        linked.append(
                            {
                                "decision_id": decision["decision_id"],
                                "parent_id": parent_id,
                                "child_id": metadata["id"],
                                "path": path,
                                "turn_id": payload["turn_id"],
                                "model": decision["requested_model"],
                            }
                        )
        session.record(f"{artifact}.json", {"decisions": decisions, "native_children": linked})
        assert linked, "No routed native child turn completed the delegated task"
        assert {row["decision_id"] for row in linked} == {
            row["decision_id"] for row in decisions
        }, "A routing decision had no matching completed child turn"
        return
    decision_ids = {decision["decision_id"] for decision in decisions}
    routed_starts = [row for row in audit if row.get("decision_id") in decision_ids]
    assert routed_starts and all(row.get("agent_id") for row in routed_starts), audit
    assert all(row.get("matches_router_decision") is not False for row in routed_starts), audit
    # Some agent versions omit the child's model from SubagentStart. The report
    # preserves that unknown value; this test claims decision + spawn + task,
    # not model-identity verification when the agent did not expose it.


def codex_routing_display_for_task(screen: str, marker: str, *, orchestrator_on: bool) -> bool:
    """Match a routing box to its tagged prompt or immediately following native spawn.

    Compaction can remove the prompt; the adjacent Spawned row still identifies
    the child. Live audit and child-answer assertions verify the actual decision.
    """
    lines = screen.splitlines()
    prompts = [index for index, line in enumerate(lines) if line.lstrip().startswith("›")]
    for index, line in enumerate(lines):
        if "Using Unity Gateway Smart Router - Subagent" not in line:
            continue
        end = index + 1
        while end < min(index + 12, len(lines)) and "└" not in lines[end]:
            end += 1
        if end >= len(lines) or "└" not in lines[end]:
            continue
        text = "".join("".join(lines[index : end + 1]).split())
        if "SelectedModel:" not in text or ("[orchestratoron]" in text) != orchestrator_on:
            continue
        preceding = [start for start in prompts if start < index]
        if preceding:
            start = preceding[-1]
            prompt_end = start + 1
            while prompt_end < index and lines[prompt_end].strip():
                prompt_end += 1
            if marker in "".join("".join(lines[start:prompt_end]).split()):
                return True
        # A compacted prompt is unavailable, but its native spawn remains directly
        # after the routing box. Do not accept later echoed answers or tool results.
        spawn = end + 1
        while spawn < len(lines) and not lines[spawn].strip():
            spawn += 1
        if spawn >= len(lines) or not lines[spawn].lstrip().startswith("• Spawned "):
            continue
        spawn_end = spawn + 1
        while spawn_end < len(lines) and lines[spawn_end].strip():
            spawn_end += 1
        if marker in "".join("".join(lines[spawn:spawn_end]).split()):
            return True
    return False
