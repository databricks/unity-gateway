"""Launch and hook diagnostics for Claude Code smart routing.

Records every routed Claude launch and every routing-hook diagnosis so reports
like ``Agent type 'ucode-route-...' not found`` can be traced to their cause:
stale router hooks in persistent settings, a different ``ug`` running the hook,
a launch that never passed ``--agents``, or a session relaunched without them.
Pure observability — nothing here changes routing behavior.

Logs live under ``~/.ucode/debug-logs/claude`` and rotate at ~1 MB. They are
always-on because affected users cannot be expected to set UCODE_DEBUG first;
one-line summaries also mirror into the opt-in ``UCODE_DEBUG`` log.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ucode.databricks import _debug
from ucode.smart_routing import routing
from ucode.smart_routing.claude_hooks import ROUTING_HOOK_COMMAND_MARKER
from ucode.telemetry import ug_version

LAUNCHES_PATH = routing.debug_log_dir("claude") / "smart-routing-launches.jsonl"
DIAGNOSTICS_PATH = routing.debug_log_dir("claude") / "smart-routing-diagnostics.jsonl"

_ANCESTOR_DEPTH = 10
_RESUME_FLAGS = ("--resume", "--continue", "-r", "-c")


def new_launch_id() -> str:
    return uuid.uuid4().hex[:12]


def record_launch(
    launch_id: str,
    *,
    launch_path: str,
    reason: str,
    catalog_source: str | None,
    model_ids: list[str],
    agent_names: list[str],
    settings_path: str | Path | None,
) -> None:
    """Append one routed-launch record with enough context to debug regressions."""
    record = {
        "launch_id": launch_id,
        "launch_path": launch_path,
        "reason": reason,
        "catalog_source": catalog_source,
        "model_ids": list(model_ids or []),
        "agent_names": list(agent_names or []),
        "settings_path": str(settings_path) if settings_path else None,
        "ucode_version": ug_version(),
        "python_executable": sys.executable,
        "ug_executable": shutil.which("ug"),
        "pid": os.getpid(),
        "at": time.time(),
    }
    routing._append_jsonl(LAUNCHES_PATH, record)
    _debug(
        "claude smart-routing launch",
        f"id={launch_id} path={launch_path} reason={reason} agents={len(record['agent_names'])}",
    )


def record_diagnostic(record: dict[str, Any]) -> None:
    """Append one hook-diagnosis record produced by :func:`diagnose`."""
    routing._append_jsonl(DIAGNOSTICS_PATH, record)
    _debug(
        "claude smart-routing diagnostic",
        f"{record.get('launch_id')} {record.get('verdict')}: {record.get('message')}",
    )


def recent_launches(limit: int = 5) -> list[dict[str, Any]]:
    return routing._read_jsonl(LAUNCHES_PATH)[-limit:]


def recent_diagnostics(limit: int = 5) -> list[dict[str, Any]]:
    return routing._read_jsonl(DIAGNOSTICS_PATH)[-limit:]


def latest_launch_id() -> str | None:
    for record in reversed(routing._read_jsonl(LAUNCHES_PATH)):
        launch_id = record.get("launch_id")
        if isinstance(launch_id, str) and launch_id:
            return launch_id
    return None


def find_claude_argv() -> list[str] | None:
    """Best-effort argv of the ancestor ``claude`` process, or None.

    Hooks are children of the Claude Code process (possibly through a shell),
    so walking process ancestors recovers the flags the session was actually
    launched with — the source of truth for its settings file and agents.
    """
    try:
        pid = os.getppid()
        for _ in range(_ANCESTOR_DEPTH):
            if pid <= 1:
                return None
            argv = _proc_cmdline(pid)
            if argv is not None:
                ppid = _proc_ppid(pid)
            else:
                argv, ppid = _ps_argv(pid)
            if _looks_like_claude(argv):
                return argv
            if ppid is None or ppid <= 1:
                return None
            pid = ppid
    except Exception:
        # Diagnostics must never break the hook they instrument.
        return None
    return None


def parse_claude_argv(argv: list[str] | None) -> dict[str, Any]:
    """Extract the routing-relevant facts from a ``claude`` launch argv."""
    tokens = [str(arg) for arg in (argv or [])]
    settings_path: str | None = None
    agents_value: str | None = None
    has_agents_flag = False
    resume = False
    index = 0
    while index < len(tokens):
        arg = tokens[index]
        if arg == "--settings" and index + 1 < len(tokens):
            settings_path = tokens[index + 1]
            index += 2
            continue
        if arg.startswith("--settings="):
            settings_path = arg.partition("=")[2]
            index += 1
            continue
        if arg == "--agents" and index + 1 < len(tokens):
            agents_value = tokens[index + 1]
            has_agents_flag = True
            index += 2
            continue
        if arg.startswith("--agents="):
            agents_value = arg.partition("=")[2]
            has_agents_flag = True
            index += 1
            continue
        if arg in _RESUME_FLAGS:
            resume = True
        index += 1
    agent_names = _agents_value_names(agents_value)
    return {
        "settings_path": settings_path,
        "has_agents_flag": has_agents_flag,
        "agent_names": agent_names,
        "routed_agent_names": [
            name for name in agent_names if name.startswith(_routed_agent_prefix())
        ],
        "resume": resume,
    }


def settings_files_with_router_hooks(extra_paths: list[str | Path] | None = None) -> list[str]:
    """Return settings files whose text still carries ucode's router hooks."""
    from ucode.agents.claude import _managed_settings_path

    claude_dir = Path.home() / ".claude"
    candidates: list[Path | None] = [
        claude_dir / "settings.json",
        claude_dir / "settings.local.json",
        claude_dir / "ucode-settings.json",
        _managed_settings_path(),
        Path.cwd() / ".claude" / "settings.json",
        Path.cwd() / ".claude" / "settings.local.json",
        *(Path(str(path)) for path in (extra_paths or [])),
    ]
    found = []
    for path in candidates:
        if path is None:
            continue
        try:
            if path.is_file() and ROUTING_HOOK_COMMAND_MARKER in path.read_text(encoding="utf-8"):
                found.append(str(path))
        except OSError:
            continue
    return list(dict.fromkeys(found))


def diagnose(
    *,
    launch_id: str | None,
    session_payload: dict[str, Any] | None = None,
    argv: list[str] | None = None,
    routed_agent: str | None = None,
) -> dict[str, Any]:
    """Collect the evidence for a routing-hook failure and classify it.

    ``argv`` (and, via :func:`find_claude_argv`, the ancestor walk) and
    ``routed_agent`` are optional so tests can inject them.
    """
    from ucode.smart_routing import v2

    payload = session_payload if isinstance(session_payload, dict) else {}
    hook_version = ug_version()
    launch_record = _latest_launch_record(launch_id)
    claude_argv = argv if argv is not None else find_claude_argv()
    parsed = parse_claude_argv(claude_argv) if claude_argv else None
    hook_settings_files = settings_files_with_router_hooks(
        [parsed["settings_path"]] if parsed and parsed["settings_path"] else []
    )

    missing_agents: list[str] = []
    if parsed is not None:
        registered = set(parsed["agent_names"])
        expected = launch_record.get("agent_names") if launch_record else None
        if isinstance(expected, list):
            missing_agents = [name for name in expected if name not in registered]
        # The hook can also rewrite to a routed agent this particular launch
        # never defined (e.g. an agent name minted by a different ucode).
        if routed_agent and routed_agent not in registered and routed_agent not in missing_agents:
            missing_agents.append(routed_agent)

    verdict, message = _verdict(
        launch_id=launch_id,
        launch_record=launch_record,
        parsed=parsed,
        hook_version=hook_version,
        hook_settings_files=hook_settings_files,
        missing_agents=missing_agents,
    )
    return {
        "launch_id": launch_id,
        "session_id": payload.get("session_id"),
        "source": payload.get("source"),
        "env": {
            v2.ENABLE_SMART_ROUTING_ENV_VAR: os.environ.get(v2.ENABLE_SMART_ROUTING_ENV_VAR),
            v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: os.environ.get(v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR),
        },
        "hook_ucode_version": hook_version,
        "launch_record": launch_record,
        "claude_argv": parsed,
        "hook_settings_files": hook_settings_files,
        "missing_agents": missing_agents,
        "routed_agent": routed_agent,
        "verdict": verdict,
        "message": message,
        "at": time.time(),
    }


def _verdict(
    *,
    launch_id: str | None,
    launch_record: dict[str, Any] | None,
    parsed: dict[str, Any] | None,
    hook_version: str,
    hook_settings_files: list[str],
    missing_agents: list[str],
) -> tuple[str, str]:
    if not launch_id:
        message = (
            "the routing hook ran without --launch-id; it is likely a stale hook "
            "left in persistent settings by an older ucode"
        )
        if hook_settings_files:
            message += f" (router hooks present in: {', '.join(hook_settings_files)})"
        return "no_launch_id", message
    if launch_record is None:
        return (
            "launch_record_missing",
            f"no launch record for id {launch_id}; this ucode ({hook_version}) "
            "did not launch the session",
        )
    launch_version = launch_record.get("ucode_version")
    if launch_version != hook_version:
        return (
            "version_mismatch",
            f"hook ucode {hook_version} differs from the launching ucode {launch_version}",
        )
    if parsed is None:
        return (
            "claude_process_not_found",
            "could not find the launching claude process, so its --settings/--agents "
            "flags are unknown",
        )
    if parsed["settings_path"] != launch_record.get("settings_path"):
        return (
            "settings_mismatch",
            f"claude --settings {parsed['settings_path']} differs from the launch record "
            f"{launch_record.get('settings_path')}",
        )
    if not parsed["has_agents_flag"]:
        return (
            "agents_flag_missing",
            "the claude process was launched without --agents",
        )
    if missing_agents:
        return (
            "agents_incomplete",
            f"routed agent(s) missing from claude --agents: {', '.join(missing_agents)}",
        )
    return "ok", "launch record, settings file, and routed agents are consistent"


def _latest_launch_record(launch_id: str | None) -> dict[str, Any] | None:
    if not launch_id:
        return None
    for record in reversed(routing._read_jsonl(LAUNCHES_PATH)):
        if record.get("launch_id") == launch_id:
            return record
    return None


def _routed_agent_prefix() -> str:
    # Imported lazily: v2 imports this module, so a module-level import would cycle.
    from ucode.smart_routing import v2

    return v2.CLAUDE_ROUTED_AGENT_PREFIX


def _agents_value_names(value: str | None) -> list[str]:
    payload = _agents_value_json(value)
    if isinstance(payload, dict):
        return [name for name in payload if isinstance(name, str)]
    return []


def _agents_value_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        pass
    try:
        return json.loads(Path(value).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _proc_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _proc_ppid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # The comm field can contain spaces and parens; the fields after the
        # final ")" are state, then ppid.
        fields = stat.rpartition(")")[2].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError):
        return None


def _ps_argv(pid: int) -> tuple[list[str] | None, int | None]:
    """Best-effort argv/ppid via ps (macOS path; /proc is preferred on Linux).

    ``ps`` space-joins the command, so arguments containing spaces cannot be
    recovered exactly — shlex splitting is the best available approximation.
    """
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", "ppid=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None, None
    try:
        parts = shlex.split(lines[0])
        return parts[1:], int(parts[0])
    except (IndexError, ValueError):
        return None, None


def _looks_like_claude(argv: list[str] | None) -> bool:
    if not argv:
        return False
    if os.path.basename(argv[0]) == "claude":
        return True
    has_launch_flag = any(
        arg == "--agents"
        or arg.startswith("--agents=")
        or arg == "--settings"
        or arg.startswith("--settings=")
        for arg in argv
    )
    return has_launch_flag and "claude" in {os.path.basename(arg) for arg in argv}
