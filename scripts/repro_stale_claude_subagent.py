#!/usr/bin/env python3
"""Reproduce CLI agent loss on Claude 2.1.248's interactive plugin refresh.

Use --interactive for a before/reload/after experiment with a real Claude TUI.
A PreToolUse hook rewrites Explore to a generated agent. CLI registration loses
that agent on /reload-plugins --force; plugin registration reloads it from disk.
Both modes use inherited child models, not the production GLM model. The default
headless four-call probe does not exercise the TUI refresh path.

Examples:
    uv run python scripts/repro_stale_claude_subagent.py --interactive --registration cli
    uv run python scripts/repro_stale_claude_subagent.py --interactive --registration plugin
    uv run python scripts/repro_stale_claude_subagent.py --analyze /tmp/run/claude.log --exit-code 0
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

PLUGIN_NAME = "ucode-smart-routing-repro"
ROUTE_AGENT = "ucode-route-glm-5-3-982d9f93"
PROMPT = """In your first assistant response, call the Agent tool exactly four times in
parallel. Use subagent_type Explore for every call. Give each agent one distinct task:
reply with A, reply with B, reply with C, and reply with D. Do not perform the tasks
yourself and do not call any other tool. After all four return, summarize their replies."""


@dataclass(frozen=True)
class Attempt:
    outcome: str
    tool_calls: int
    returncode: int | None
    log_path: Path
    registration_evidence: str
    routed_starts: int
    completions: int
    reason: str


def _agent_definition(index: int) -> dict[str, object]:
    return {
        "description": f"Registration race reproduction agent {index}",
        "prompt": "Reply with exactly the requested letter and nothing else.",
        "model": "inherit",
        "tools": [],
    }


def _agent_names(count: int) -> list[str]:
    return [ROUTE_AGENT, *(f"ucode-route-repro-{index:03d}" for index in range(count - 1))]


def _write_plugin(plugin_dir: Path, count: int) -> None:
    manifest_dir = plugin_dir / ".claude-plugin"
    agents_dir = plugin_dir / "agents"
    manifest_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": PLUGIN_NAME,
                "version": "1.0.0",
                "description": "Parallel agent registration race reproduction.",
                "author": {"name": "Databricks"},
            }
        )
    )
    for index, name in enumerate(_agent_names(count)):
        definition = _agent_definition(index)
        (agents_dir / f"{name}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"name: {json.dumps(name)}",
                    f"description: {json.dumps(definition['description'])}",
                    "model: inherit",
                    "tools: []",
                    "---",
                    "",
                    str(definition["prompt"]),
                    "",
                ]
            )
        )


def _write_hook(hook_path: Path) -> None:
    hook_path.write_text(
        """#!/usr/bin/env python3
import json
import sys

payload = json.load(sys.stdin)
updated = dict(payload["tool_input"])
updated.pop("model", None)
updated["subagent_type"] = sys.argv[1]
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": updated,
    }
}))
"""
    )


def _write_settings(
    settings_path: Path,
    hook_path: Path,
    routed_name: str,
    base_settings: dict[str, object],
) -> None:
    settings = dict(base_settings)
    settings["hooks"] = {
        "PreToolUse": [
            {
                "matcher": "Agent|Task",
                "hooks": [
                    {
                        "type": "command",
                        "command": shlex.join([sys.executable, str(hook_path), routed_name]),
                        "timeout": 10,
                    }
                ],
            }
        ]
    }
    settings_path.write_text(json.dumps(settings))


def _ucode_claude_settings(state_path: Path) -> dict[str, object]:
    if not state_path.is_file():
        return {}
    state = json.loads(state_path.read_text())
    current_workspace = state.get("current_workspace")
    workspaces = state.get("workspaces")
    if not isinstance(current_workspace, str) or not isinstance(workspaces, dict):
        return {}
    workspace = workspaces.get(current_workspace)
    if not isinstance(workspace, dict):
        return {}
    agents = workspace.get("agents")
    claude = agents.get("claude") if isinstance(agents, dict) else None
    if not isinstance(claude, dict):
        return {}
    settings: dict[str, object] = {}
    auth_command = claude.get("auth_command")
    if isinstance(auth_command, str):
        settings["apiKeyHelper"] = auth_command
    env = claude.get("env")
    if isinstance(env, dict):
        configured_env = dict(env)
        configured_env.setdefault(
            "ANTHROPIC_CUSTOM_HEADERS", "x-databricks-use-coding-agent-mode: true"
        )
        claude_models = workspace.get("claude_models")
        if isinstance(claude_models, dict):
            for family in ("opus", "sonnet", "haiku"):
                model = claude_models.get(family)
                if isinstance(model, str):
                    configured_env[f"ANTHROPIC_DEFAULT_{family.upper()}_MODEL"] = model
            settings["modelOverrides"] = {
                model.removeprefix("system.ai."): model
                for model in claude_models.values()
                if isinstance(model, str) and model.startswith("system.ai.claude-")
            }
        settings["env"] = configured_env
    return settings


def _message_blocks(event: dict) -> list[dict]:
    content = event.get("message", {}).get("content", [])
    return (
        [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    )


def _classify(
    output: str, returncode: int | None, log_path: Path, routed_name: str = ROUTE_AGENT
) -> Attempt:
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    init = next(
        (
            event
            for event in events
            if event.get("type") == "system" and event.get("subtype") == "init"
        ),
        None,
    )
    registration_evidence = "unknown"
    if init is not None and isinstance(init.get("agents"), list):
        registration_evidence = (
            "present_at_first_init" if routed_name in init["agents"] else "absent_at_first_init"
        )
    assistants = [
        event
        for event in events
        if event.get("type") == "assistant" and event.get("parent_tool_use_id") is None
    ]
    first_message = assistants[0].get("message", {}).get("id") if assistants else None
    first_calls = {}
    all_calls = {}
    for event in assistants:
        for block in _message_blocks(event):
            if block.get("type") == "tool_use" and block.get("name") in {"Agent", "Task"}:
                all_calls[block["id"]] = block
                if first_message and event.get("message", {}).get("id") == first_message:
                    first_calls[block["id"]] = block

    errors = []
    for event in events:
        if event.get("type") == "user" and event.get("parent_tool_use_id") is None:
            for block in _message_blocks(event):
                if (
                    block.get("type") == "tool_result"
                    and block.get("is_error")
                    and block.get("tool_use_id") in all_calls
                ):
                    errors.append(json.dumps(block.get("content", "")))
        if (
            event.get("type") == "system"
            and event.get("subtype") == "task_notification"
            and event.get("tool_use_id") in all_calls
            and event.get("status") == "failed"
        ):
            errors.append(str(event.get("summary", "")))

    starts = {
        event["tool_use_id"]
        for event in events
        if event.get("type") == "system"
        and event.get("subtype") == "task_started"
        and event.get("tool_use_id") in first_calls
        and event.get("subagent_type") == routed_name
        and event.get("spawn_depth") == 1
    }
    completions = {
        event["tool_use_id"]
        for event in events
        if event.get("type") == "system"
        and event.get("subtype") == "task_notification"
        and event.get("tool_use_id") in starts
        and event.get("status") == "completed"
    }
    final_results = [event for event in events if event.get("type") == "result"]
    outcome = "inconclusive"
    reason = "Require four Explore calls in the first message, routed starts, and completions."
    if any(f"Agent type '{routed_name}' not found" in error for error in errors):
        outcome = "registry_failure"
        reason = "Claude reported the missing route agent in an actual tool failure."
    elif errors:
        reason = "Agent tool failed for another reason; inspect the log."
    elif (
        returncode == 0
        and registration_evidence == "present_at_first_init"
        and len(first_calls) == len(all_calls) == len(starts) == len(completions) == 4
        and all(
            call.get("input", {}).get("subagent_type") == "Explore" for call in first_calls.values()
        )
        and final_results
        and all(
            event.get("subtype") == "success" and event.get("is_error") is False
            for event in final_results
        )
    ):
        outcome = "no_failure_observed"
        reason = "All four routed children completed; this does not rule out a race."
    return Attempt(
        outcome,
        len(all_calls),
        returncode,
        log_path,
        registration_evidence,
        len(starts),
        len(completions),
        reason,
    )


def _classify_reload(attempt_dir: Path, routed_name: str, returncode: int | None) -> Attempt:
    transcripts = list((attempt_dir / "claude-config/projects").glob("*/*.jsonl"))
    debug_path = attempt_dir / "debug.log"
    debug = debug_path.read_text() if debug_path.is_file() else ""
    phase = "before"
    markers = {"before": "BEFORE_RELOAD", "after": "AFTER_RELOAD"}
    calls = {}
    launched = {}
    completed = set()
    missing = set()
    reload_requested = False
    if len(transcripts) == 1:
        for line in transcripts[0].read_text().splitlines():
            event = json.loads(line)
            if event.get("isSidechain"):
                continue
            content = event.get("message", {}).get("content", "")
            if event.get("type") == "user" and isinstance(content, str):
                if "<command-name>/reload-plugins</command-name>" in content:
                    reload_requested = True
                if content.startswith("<task-notification>"):
                    try:
                        notification = ET.fromstring(content)
                    except ET.ParseError:
                        continue
                    tool_id = notification.findtext("tool-use-id")
                    agent_id = launched.get(tool_id)
                    if (
                        agent_id
                        and calls.get(tool_id) == phase
                        and notification.findtext("status") == "completed"
                        and notification.findtext("result", "").strip() == markers[phase]
                        and f"agentId={agent_id} agentType={routed_name} exitPath=completed"
                        in debug
                    ):
                        completed.add(tool_id)
            if (
                reload_requested
                and event.get("type") == "system"
                and event.get("subtype") == "local_command"
                and str(event.get("content", "")).startswith("<local-command-stdout>Reloaded:")
                and "refreshActivePlugins:" in debug
            ):
                phase = "after"
            for block in _message_blocks(event):
                if (
                    event.get("type") == "assistant"
                    and block.get("type") == "tool_use"
                    and block.get("name") in {"Agent", "Task"}
                ):
                    inputs = block.get("input", {})
                    calls[block["id"]] = (
                        phase
                        if inputs.get("subagent_type") == "Explore"
                        and markers[phase] in inputs.get("prompt", "")
                        else "unexpected"
                    )
                if event.get("type") == "user" and block.get("type") == "tool_result":
                    tool_id = block.get("tool_use_id")
                    result = event.get("toolUseResult")
                    if isinstance(result, dict) and result.get("agentId"):
                        launched[tool_id] = result["agentId"]
                    if block.get("is_error") and f"Agent type '{routed_name}' not found" in str(
                        block.get("content")
                    ):
                        missing.add(tool_id)
    before = {tool_id for tool_id, call_phase in calls.items() if call_phase == "before"}
    after = {tool_id for tool_id, call_phase in calls.items() if call_phase == "after"}
    outcome = "inconclusive"
    reason = "Require a routed completion before reload, reload completion, and one call after it."
    if len(calls) == 2 and len(before) == len(after) == 1 and before <= completed:
        if after <= missing:
            outcome = "registry_failure"
            reason = "Routed child completed before plugin refresh; the same route was missing afterward."
        elif after <= completed and returncode == 0:
            outcome = "no_failure_observed"
            reason = (
                "Routed children completed both before and after the interactive plugin refresh."
            )
    return Attempt(
        outcome,
        len(calls),
        returncode,
        debug_path,
        "proved_before_refresh" if before and before <= completed else "unknown",
        len(launched),
        len(completed),
        reason,
    )


def run_attempt(
    claude: str,
    registration: str,
    agent_count: int,
    model: str,
    timeout: int,
    log_dir: Path,
    attempt_number: int,
    base_settings: dict[str, object],
    interactive: bool = False,
) -> Attempt:
    attempt_dir = log_dir / f"{registration}-{attempt_number:02d}"
    attempt_dir.mkdir(parents=True)
    hook_path = attempt_dir / "route_hook.py"
    settings_path = attempt_dir / "settings.json"
    _write_hook(hook_path)

    routed_name = ROUTE_AGENT
    registration_args: list[str]
    if registration == "cli":
        definitions = {
            name: _agent_definition(index) for index, name in enumerate(_agent_names(agent_count))
        }
        registration_args = ["--agents", json.dumps(definitions, separators=(",", ":"))]
    else:
        plugin_dir = attempt_dir / "plugin"
        _write_plugin(plugin_dir, agent_count)
        routed_name = f"{PLUGIN_NAME}:{ROUTE_AGENT}"
        registration_args = ["--plugin-dir", str(plugin_dir)]
    _write_settings(settings_path, hook_path, routed_name, base_settings)

    if interactive:
        config_dir = attempt_dir / "claude-config"
        config_dir.mkdir()
        command = [
            claude,
            "--model",
            model,
            "--settings",
            str(settings_path),
            "--setting-sources",
            "",
            "--debug-file",
            str(attempt_dir / "debug.log"),
            "--tools",
            "Agent",
            "--allowed-tools",
            "Agent",
            *registration_args,
        ]
        print(
            "Interactive plugin-refresh experiment (real model requests):\n"
            "1. Finish visible onboarding/trust prompts for this disposable directory.\n"
            "2. Ask: Use one Explore subagent to reply with exactly BEFORE_RELOAD.\n"
            "3. Wait for the subagent to finish, then enter: /reload-plugins --force\n"
            "4. Wait for Reloaded, then ask: Use one Explore subagent to reply with exactly AFTER_RELOAD.\n"
            "5. Record the actual tool result and exit with /exit.\n"
            f"Registration: {registration}; agent: {routed_name}\n"
            f"Evidence directory: {attempt_dir}\n"
            "This tests plugin refresh; it does not establish what triggered the user's incident.",
            flush=True,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=attempt_dir,
                env={
                    **os.environ,
                    "CLAUDE_CONFIG_DIR": str(config_dir),
                    "DISABLE_AUTOUPDATER": "1",
                },
                timeout=timeout,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
        return _classify_reload(attempt_dir, routed_name, returncode)

    command = [
        claude,
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--max-turns",
        "3",
        "--model",
        model,
        "--settings",
        str(settings_path),
        "--allowed-tools",
        "Agent",
        *registration_args,
        PROMPT,
    ]
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = completed.stdout
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        captured = exc.stdout or ""
        output = captured.decode(errors="replace") if isinstance(captured, bytes) else captured
        output += "\nTIMED OUT"
        returncode = 124
    log_path = attempt_dir / "claude.log"
    log_path.write_text(output)
    return _classify(output, returncode, log_path, routed_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registration", choices=("cli", "plugin"), default="cli")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Open an isolated real TUI for the before/reload/after experiment (one attempt)",
    )
    parser.add_argument(
        "--analyze",
        type=Path,
        nargs="+",
        help="Inspect existing JSONL logs without launching Claude",
    )
    parser.add_argument(
        "--analyze-reload",
        type=Path,
        nargs="+",
        help="Inspect existing interactive attempt directories without launching Claude",
    )
    parser.add_argument(
        "--exit-code", type=int, help="Known process exit code for --analyze; otherwise unknown"
    )
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--agent-count", type=int, default=1)
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--timeout", type=int, default=480)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--ucode-state", type=Path, default=Path.home() / ".ucode/state.json")
    args = parser.parse_args()
    if args.attempts < 1 or args.agent_count < 1 or args.timeout < 1:
        parser.error("--attempts, --agent-count, and --timeout must be positive")
    if args.interactive and (args.attempts != 1 or args.analyze or args.analyze_reload):
        parser.error("--interactive requires one attempt and cannot be combined with analysis")
    if args.analyze and args.analyze_reload:
        parser.error("Choose --analyze or --analyze-reload")
    if args.analyze_reload:
        routed_name = ROUTE_AGENT if args.registration == "cli" else f"{PLUGIN_NAME}:{ROUTE_AGENT}"
        return _report(
            [_classify_reload(path, routed_name, args.exit_code) for path in args.analyze_reload]
        )

    if args.analyze:
        routed_name = ROUTE_AGENT if args.registration == "cli" else f"{PLUGIN_NAME}:{ROUTE_AGENT}"
        attempts = [
            _classify(path.read_text(), args.exit_code, path, routed_name) for path in args.analyze
        ]
        return _report(attempts)

    log_dir = args.log_dir or Path(tempfile.mkdtemp(prefix="claude-agent-race-"))
    log_dir.mkdir(parents=True, exist_ok=True)
    base_settings = _ucode_claude_settings(args.ucode_state)
    attempts = []
    for attempt_number in range(1, args.attempts + 1):
        attempt = run_attempt(
            args.claude,
            args.registration,
            args.agent_count,
            args.model,
            args.timeout,
            log_dir,
            attempt_number,
            base_settings,
            args.interactive,
        )
        attempts.append(attempt)
        print(
            f"{attempt_number}/{args.attempts}: {attempt.outcome} "
            f"(Agent calls={attempt.tool_calls}, exit={attempt.returncode})",
            flush=True,
        )
    return _report(attempts)


def _report(attempts: list[Attempt]) -> int:
    for attempt in attempts:
        print(
            f"{attempt.log_path}: {attempt.outcome}; {attempt.registration_evidence}; "
            f"routed starts={attempt.routed_starts}; completions={attempt.completions}. "
            f"{attempt.reason}",
            flush=True,
        )

    counts = {
        outcome: sum(attempt.outcome == outcome for attempt in attempts)
        for outcome in ("registry_failure", "no_failure_observed", "inconclusive")
    }
    print(f"Results: {counts}", flush=True)
    if counts["registry_failure"]:
        return 1
    return 2 if counts["inconclusive"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
