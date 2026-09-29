#!/usr/bin/env python3
"""Snapshot an existing Isaac/Claude routing failure; Python 3.12+, no dependencies.

Run inside the affected session, or pass --pid for its Claude process. Keep that
session open: generated agents use --agents or a launch-scoped --plugin-dir.
Nothing is launched or reconfigured. Only the output directory is written.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

LIMIT = 2 * 1024 * 1024
MAX_FILES = 300
ENV_KEYS = (
    "CLAUDE_CONFIG_DIR",
    "ENABLE_SMART_ROUTING_V2",
    "ENABLE_SMART_ROUTING_SUBAGENT_ONLY",
    "SMART_ROUTER_NAME",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY",
    "ISAAC_ENABLE_UG",
    "ISAAC_DEFAULT_OMNI",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
MISSING_AGENT = re.compile(
    r"Agent type '([\w:.-]+)' not found\. Available agents:\s*([\w:.-]+(?:,\s*[\w:.-]+)*)"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def routed_name(model: str) -> str:
    """Mirror UG's naming algorithm without importing or installing UG."""
    canonical = (
        model.replace("databricks-claude-", "system.ai.claude-", 1)
        if model.startswith("databricks-claude-")
        else model
    )
    normalized = canonical.rsplit("/", 1)[-1]
    for prefix in ("databricks-", "system.ai."):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    safe = "".join(c if c.isalnum() else "-" for c in normalized.lower())
    slug = "-".join(part for part in safe.split("-") if part)
    return f"ucode-route-{slug[:36]}-{digest(canonical.encode())[:8]}"


def read_file(path: Path) -> tuple[dict, bytes | None]:
    meta: dict = {"path": str(path)}
    try:
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            meta.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
            data = handle.read(LIMIT + 1)
        if len(data) > LIMIT:
            return {**meta, "status": "too_large"}, None
        return {**meta, "status": "ok", "sha256": digest(data)}, data
    except OSError as exc:
        return {**meta, "status": type(exc).__name__}, None


def read_json(path: Path) -> tuple[dict, dict]:
    meta, data = read_file(path)
    if data is not None:
        try:
            doc = json.loads(data)
            if isinstance(doc, dict):
                return meta, doc
            meta["status"] = "not_an_object"
        except (ValueError, UnicodeError):
            meta["status"] = "invalid_json"
    return meta, {}


def option_values(argv: list[str], option: str) -> list[str]:
    result = []
    for i, arg in enumerate(argv):
        if arg == "--":
            break
        if arg.startswith(option + "="):
            result.append(arg.split("=", 1)[1])
        elif arg == option and i + 1 < len(argv):
            result.append(argv[i + 1])
    return result


def summarize_agents(raw: str) -> dict:
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"status": "invalid_json"}
    if not isinstance(doc, dict):
        return {"status": "not_an_object"}
    return {
        "status": "ok",
        "agents": {
            name: {"model": value.get("model") if isinstance(value.get("model"), str) else None}
            for name, value in doc.items()
            if isinstance(value, dict)
        },
    }


def summarize_settings(doc: dict) -> dict:
    result = {
        key: doc[key]
        for key in ("model", "disableAllHooks", "allowManagedHooksOnly")
        if isinstance(doc.get(key), (str, bool))
    }
    env = doc.get("env")
    result["env"] = {
        k: env[k] for k in ENV_KEYS if isinstance(env, dict) and isinstance(env.get(k), str)
    }
    permissions = doc.get("permissions")
    if isinstance(permissions, dict):
        result["agent_permissions"] = {
            key: [
                rule
                for rule in permissions[key]
                if isinstance(rule, str) and re.fullmatch(r"(?:Agent|Task)(?:\([\w:*.-]+\))?", rule)
            ]
            for key in ("allow", "deny", "ask")
            if isinstance(permissions.get(key), list)
        }
    picker = doc.get("modelPicker")
    if isinstance(picker, dict):
        result["modelPicker"] = {
            "replaceBuiltInOptions": picker.get("replaceBuiltInOptions") is True,
            "models": [
                row["model"]
                for row in picker.get("options", [])
                if isinstance(row, dict) and isinstance(row.get("model"), str)
            ]
            if isinstance(picker.get("options"), list)
            else [],
        }
    for key in ("modelOverrides", "enabledPlugins"):
        value = doc.get(key)
        if isinstance(value, dict):
            result[key] = {k: v for k, v in value.items() if isinstance(v, (str, bool))}
    result["hooks"] = []
    hooks = doc.get("hooks")
    for event, groups in hooks.items() if isinstance(hooks, dict) else []:
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for hook in group["hooks"]:
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command")
                entry = {"event": event, "matcher": group.get("matcher"), "type": hook.get("type")}
                if isinstance(command, str):
                    entry["command_sha256"] = digest(command.encode())
                    try:
                        args = shlex.split(command)
                    except ValueError:
                        args = []
                        entry["parse_status"] = "invalid_shell_quoting"
                    # Parse the known generated command only; never execute a hook.
                    if len(args) >= 3 and args[1] == "claude-router-hook":
                        entry.update(
                            executable=args[0],
                            routing_event=args[2],
                            models=option_values(args, "--model"),
                        )
                result["hooks"].append(entry)
    return result


def run_readonly(args: list[str]) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def process_table() -> dict[int, dict]:
    result = {}
    for line in run_readonly(["ps", "-axo", "pid=,ppid=,comm="]).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            pid, parent = int(parts[0]), int(parts[1])
            result[pid] = {"pid": pid, "ppid": parent, "executable": parts[2]}
    return result


def is_claude(proc: dict) -> bool:
    executable = proc.get("executable", "")
    return Path(executable).name == "claude" or "/claude/versions/" in executable


def select_process(table: dict[int, dict], explicit: int | None, parent: int) -> int | None:
    if explicit is not None:
        return explicit
    visited = set()
    while parent in table and parent not in visited:
        visited.add(parent)
        if is_claude(table[parent]):
            return parent
        parent = table[parent]["ppid"]
    return None


def mac_process_data(pid: int) -> tuple[list[str], dict[str, str]]:
    # KERN_PROCARGS2 returns argc, executable, NUL padding, argv, then environ.
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(LIMIT)
    buffer = ctypes.create_string_buffer(LIMIT)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "process arguments unavailable")
    return parse_mac_process_data(buffer.raw[: size.value])


def parse_mac_process_data(data: bytes) -> tuple[list[str], dict[str, str]]:
    argc = int.from_bytes(data[:4], sys.byteorder, signed=True)
    if not 0 < argc < 100000:
        raise ValueError("invalid argc")
    pos = data.index(b"\0", 4) + 1
    while pos < len(data) and data[pos] == 0:
        pos += 1
    fields = data[pos:].decode(errors="replace").split("\0")
    if len(fields) < argc:
        raise ValueError("truncated argv")
    return fields[:argc], dict(item.split("=", 1) for item in fields[argc:] if "=" in item)


def process_data(pid: int) -> tuple[dict, list[str], dict[str, str]]:
    result: dict = {"pid": pid}
    args: list[str] = []
    env: dict[str, str] = {}
    try:
        if sys.platform == "darwin":
            args, env = mac_process_data(pid)
            # lsof's machine-readable name field preserves spaces in the cwd.
            cwd = run_readonly(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
            result["cwd"] = next(
                (line[1:] for line in cwd.splitlines() if line.startswith("n")), None
            )
        else:
            root = Path("/proc") / str(pid)
            args = (root / "cmdline").read_bytes().decode(errors="replace").rstrip("\0").split("\0")
            result["cwd"] = os.readlink(root / "cwd")
            result["resolved_executable"] = os.readlink(root / "exe")
            try:
                env = dict(
                    item.split("=", 1)
                    for item in (root / "environ").read_bytes().decode(errors="replace").split("\0")
                    if "=" in item
                )
            except OSError as exc:
                result["environment_status"] = type(exc).__name__
        result["status"] = "ok"
    except (OSError, ValueError) as exc:
        result["status"] = type(exc).__name__
    result["started"] = run_readonly(["ps", "-p", str(pid), "-o", "lstart="]).strip() or None
    result["argument_count"] = len(args)
    result["argument_source"] = "current_process_arguments_not_exec_history"
    result["env"] = {key: env[key] for key in ENV_KEYS if key in env}
    result["agents_arguments"] = [summarize_agents(raw) for raw in option_values(args, "--agents")]
    result["launch_options"] = {
        key: option_values(args, key)
        for key in (
            "--model",
            "--agent",
            "--session-id",
            "--resume",
            "--setting-sources",
            "--plugin-dir",
        )
        if option_values(args, key)
    }
    result["continue"] = "--continue" in args or "-c" in args
    return result, args, env


def inventory_agents(root: Path) -> dict:
    records = []
    truncated = False
    if root.is_dir():
        for path in root.rglob("*.md"):
            if len(records) >= MAX_FILES:
                truncated = True
                break
            meta, data = read_file(path)
            if data is not None:
                text = data.decode(errors="replace")
                if text.startswith("---\n") and "\n---" in text[4:]:
                    front = text[4:].split("\n---", 1)[0]
                    for key in ("name", "model"):
                        match = re.search(rf"^{key}:\s*([^\n]+)$", front, re.MULTILINE)
                        if match:
                            meta[key] = match[1].strip().strip("\"'")
            records.append(meta)
    return {"path": str(root), "exists": root.is_dir(), "files": records, "truncated": truncated}


def log_events(path: Path, session_id: str, *, transcript: bool = False) -> dict:
    result: dict = {"path": str(path), "events": [], "malformed_lines": 0}
    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - LIMIT)
            handle.seek(start)
            if start:
                handle.readline()  # a possibly partial first line
            data = handle.read(LIMIT)
        result.update(status="ok", tail_truncated=bool(start), size=size)
    except OSError as exc:
        result["status"] = type(exc).__name__
        return result
    for raw in data.splitlines():
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            result["malformed_lines"] += 1
            continue
        if not isinstance(row, dict) or row.get("sessionId", row.get("session_id")) != session_id:
            continue
        if not transcript:
            result["events"].append(
                {
                    key: row[key]
                    for key in (
                        "session_id",
                        "at",
                        "decision_id",
                        "agent_id",
                        "agent_type",
                        "model",
                        "router_model",
                        "requested_model",
                        "matches_router_decision",
                    )
                    if key in row and isinstance(row[key], (str, int, float, bool, type(None)))
                }
            )
            continue
        message = row.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            base = {"timestamp": row.get("timestamp"), "session_id": session_id}
            if (
                message.get("role") == "assistant"
                and block.get("type") == "tool_use"
                and block.get("name") in ("Agent", "Task")
            ):
                inputs = block.get("input")
                if isinstance(inputs, dict):
                    result["events"].append(
                        {
                            **base,
                            "event": "spawn_call",
                            "tool_use_id": block.get("id"),
                            **{
                                k: inputs[k]
                                for k in ("subagent_type", "model", "run_in_background")
                                if k in inputs and isinstance(inputs[k], (str, bool))
                            },
                        }
                    )
            if block.get("type") != "tool_result" or block.get("is_error") is not True:
                continue
            content = block.get("content")
            texts = (
                [content]
                if isinstance(content, str)
                else [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ]
                if isinstance(content, list)
                else []
            )
            for text in texts:
                match = MISSING_AGENT.search(text)
                if match:
                    # Export agent identifiers only, never the remainder of the error.
                    available = re.findall(r"[\w:.-]+", match[2])
                    result["events"].append(
                        {
                            **base,
                            "event": "agent_type_not_found",
                            "tool_use_id": block.get("tool_use_id"),
                            "missing_agent": match[1],
                            "available_agents": available,
                        }
                    )
    return result


def subagent_evidence(transcript: Path) -> dict:
    """Keep the selected session's agent type and response model, never message text."""
    root = transcript.with_suffix("") / "subagents"
    records = []
    truncated = False
    for path in root.glob("*.meta.json"):
        if len(records) >= MAX_FILES:
            truncated = True
            break
        meta, doc = read_json(path)
        entry = {
            **meta,
            **{
                k: doc[k]
                for k in ("agentType", "toolUseId", "spawnDepth")
                if isinstance(doc.get(k), (str, int))
            },
        }
        child_path = path.with_name(path.name.removesuffix(".meta.json") + ".jsonl")
        child_meta, data = read_file(child_path)
        models = set()
        if data:
            for line in data.splitlines():
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                message = row.get("message") if isinstance(row, dict) else None
                if (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and isinstance(message.get("model"), str)
                ):
                    models.add(message["model"])
        entry["transcript"] = child_meta
        entry["response_models"] = sorted(models)
        entry["tool_evidence"] = log_events(child_path, transcript.stem, transcript=True)
        records.append(entry)
    return {"path": str(root), "agents": records, "truncated": truncated}


def file_stat(path: Path) -> dict:
    try:
        stat = path.stat()
        return {"status": "ok", "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError as exc:
        return {"status": type(exc).__name__}


def compare_registry(process: dict | None, settings: list[dict]) -> list[dict]:
    findings = []
    supplied: dict = {}
    known = bool(process and process.get("status") == "ok")
    plugin_dirs = (process or {}).get("launch_options", {}).get("--plugin-dir", [])
    if plugin_dirs:
        known = False
        findings.append(
            {
                "check": "launch_plugin_agents",
                "status": "unknown",
                "paths": plugin_dirs,
                "note": "Plugin paths supplied; this collector does not resolve their agent registry. Missing --agents alone is not a missing definition.",
            }
        )
    for value in (process or {}).get("agents_arguments", []):
        supplied.update(value.get("agents", {}))
        if value.get("status") != "ok":
            known = False
    active = [s for s in settings if s.get("scope") == "launch"]
    for source in active:
        for hook in source.get("settings", {}).get("hooks", []):
            if hook.get("routing_event") != "route-subagent":
                continue
            models = hook.get("models", [])
            missing = [routed_name(m) for m in models if routed_name(m) not in supplied]
            mismatched = [
                routed_name(m)
                for m in models
                if routed_name(m) in supplied
                and supplied[routed_name(m)].get("model")
                != (
                    "system.ai.claude-" + m[len("databricks-claude-") :]
                    if m.startswith("databricks-claude-")
                    else m
                )
            ]
            findings.append(
                {
                    "check": "launch_hook_vs_supplied_agents",
                    "source": source["path"],
                    "status": "unknown"
                    if not known
                    else "mismatch"
                    if missing or mismatched
                    else "match",
                    "missing_agents": missing if known else [],
                    "model_mismatches": mismatched if known else [],
                    "hook_models": models,
                }
            )
    routing_hooks = [
        h
        for s in settings
        for h in s.get("settings", {}).get("hooks", [])
        if h.get("routing_event") == "route-subagent"
    ]
    if len(routing_hooks) > 1:
        findings.append(
            {
                "check": "multiple_routing_hook_sources",
                "status": "inspect",
                "count": len(routing_hooks),
                "note": "Source inventory only; Claude may merge or deduplicate hooks.",
            }
        )
    if not active:
        findings.append({"check": "launch_settings", "status": "unavailable"})
    return findings


def collect(pid: int | None = None, session_id: str | None = None) -> dict:
    home = Path.home()
    table = process_table()
    selected = select_process(table, pid, os.getppid())
    report: dict = {
        "schema_version": 1,
        "collected_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "claude_candidates": [v for v in table.values() if is_claude(v)],
        "limitations": [
            "--agents is launch evidence, not a readout of Claude's in-memory registry.",
            "Launch-scoped --plugin-dir can supply agents without --agents. Its registry is not resolved by this collector.",
            "Snapshot only: earlier reloads and removed per-launch settings may be unavailable.",
            "Current process arguments can be rewritten or unavailable; absent flags do not prove they were absent at exec. PPID 1 can indicate reparenting, not a direct systemd launch.",
            "Session logs can span multiple process lifetimes or resumes. Compare event timestamps with the selected process start time before attributing failures to this process.",
        ],
    }
    args: list[str] = []
    env = os.environ
    process = None
    if selected is not None:
        process, args, env = process_data(selected)
        report["process"] = process
        if process.get("status") != "ok":
            report["limitations"].append(
                f"Selected process could not be inspected ({process.get('status')}); keep it open and verify --pid and OS permissions."
            )
        if not process.get("cwd"):
            report["limitations"].append(
                "Target cwd unavailable; project settings and session candidates use the collector's cwd."
            )
        if process.get("environment_status"):
            report["limitations"].append(
                "Target environment unavailable; configuration-directory overrides and executable resolution are incomplete."
            )
        ancestry = []
        ancestor = selected
        while ancestor in table and ancestor not in {p["pid"] for p in ancestry}:
            ancestry.append(table[ancestor])
            ancestor = table[ancestor]["ppid"]
        report["ancestry"] = ancestry
    else:
        report["limitations"].append(
            "No Claude ancestor selected. Pass --pid from the candidate list to collect launch evidence."
        )
    if env.get("HOME"):
        home = Path(env["HOME"])
    cwd = Path(process["cwd"]) if process and process.get("cwd") else Path.cwd()
    config = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
    if not config.is_absolute():
        config = cwd / config
    report["paths"] = {
        "cwd": str(cwd),
        "claude_config": str(config),
        "ug_config": str(home / ".ucode"),
    }
    report["executables"] = {}
    for name in ("isaac", "ug", "ucode", "claude"):
        resolved = shutil.which(name, path=env.get("PATH", os.defpath))
        entry: dict = {"path": resolved}
        if resolved:
            actual = Path(resolved).resolve()
            entry["resolved"] = str(actual)
            entry.update(file_stat(actual))
            if "/claude/versions/" in str(actual):
                entry["version"] = actual.name
            if name in ("ug", "ucode"):
                for meta in actual.parent.parent.glob(
                    "lib/python*/site-packages/*gateway*.dist-info/METADATA"
                ):
                    _, data = read_file(meta)
                    if data and (match := re.search(rb"^Version: (.+)$", data, re.MULTILINE)):
                        entry["version"] = match[1].decode(errors="replace")
        report["executables"][name] = entry
    report["limitations"].append(
        "Executable lookup uses the selected process PATH when readable; Isaac build version may require its own version command, which this snapshot does not run."
    )
    sources = [
        (
            "managed",
            Path("/Library/Application Support/ClaudeCode/managed-settings.json")
            if sys.platform == "darwin"
            else Path("/etc/claude-code/managed-settings.json"),
        ),
        ("ug", home / ".claude/ucode-settings.json"),
        ("user", config / "settings.json"),
    ]
    for parent in reversed((cwd, *cwd.parents)):
        for filename in ("settings.json", "settings.local.json"):
            path = parent / ".claude" / filename
            if path.is_file() and path != config / "settings.json":
                sources.append(("project", path))
    settings = []
    for scope, path in sources:
        meta, doc = read_json(path)
        settings.append({**meta, "scope": scope, "settings": summarize_settings(doc)})
    for raw in option_values(args, "--settings"):
        if raw.lstrip().startswith("{"):
            try:
                doc = json.loads(raw)
                settings.append(
                    {
                        "path": "<inline>",
                        "scope": "launch",
                        "settings": summarize_settings(doc) if isinstance(doc, dict) else {},
                        "status": "ok" if isinstance(doc, dict) else "not_an_object",
                    }
                )
            except ValueError:
                settings.append({"path": "<inline>", "scope": "launch", "status": "invalid_json"})
        else:
            path = Path(raw).expanduser()
            meta, doc = read_json(path if path.is_absolute() else cwd / path)
            settings.append({**meta, "scope": "launch", "settings": summarize_settings(doc)})
    report["settings"] = settings
    report["findings"] = compare_registry(process, settings)
    report["hook_executables"] = []
    for executable in sorted(
        {
            h["executable"]
            for s in settings
            for h in s.get("settings", {}).get("hooks", [])
            if "executable" in h
        }
    ):
        resolved = (
            executable
            if Path(executable).is_absolute()
            else shutil.which(executable, path=env.get("PATH", os.defpath))
        )
        if resolved:
            meta, _ = read_file(Path(resolved))
            report["hook_executables"].append({**meta, "requested": executable})
        else:
            report["hook_executables"].append({"requested": executable, "status": "not_on_path"})
    meta, state = read_json(home / ".ucode/state.json")
    current = state.get("current_workspace")
    workspaces = state.get("workspaces")
    workspace = (
        workspaces.get(current, {})
        if isinstance(workspaces, dict) and isinstance(current, str)
        else {}
    )
    report["ug_state"] = (
        {
            **meta,
            "workspace_count": len(workspaces) if isinstance(workspaces, dict) else 0,
            "current_workspace_sha256": digest(current.encode())
            if isinstance(current, str)
            else None,
            "routing": {
                k: workspace[k]
                for k in (
                    "claude_models",
                    "smart_routing_enabled",
                    "claude_relayed",
                    "available_tools",
                )
                if k in workspace
            },
        }
        if isinstance(workspace, dict)
        else meta
    )
    roots = {
        config / "agents",
        *(p / ".claude/agents" for p in (cwd, *cwd.parents) if (p / ".claude/agents").is_dir()),
    }
    meta, plugins = read_json(config / "plugins/installed_plugins.json")
    report["plugins"] = {**meta, "installed": []}
    installed = plugins.get("plugins")
    for name, entries in installed.items() if isinstance(installed, dict) else []:
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                report["plugins"]["installed"].append(
                    {
                        "name": name,
                        **{
                            k: entry[k]
                            for k in ("scope", "version", "installPath")
                            if isinstance(entry.get(k), str)
                        },
                    }
                )
                if isinstance(entry.get("installPath"), str):
                    roots.add(Path(entry["installPath"]) / "agents")
    report["agent_files"] = [inventory_agents(root) for root in sorted(roots)]
    if session_id is None:
        ids = {
            value
            for flag in ("--session-id", "--resume", "-r")
            for value in option_values(args, flag)
            if UUID.fullmatch(value)
        }
        if len(ids) == 1:
            session_id = ids.pop()
        elif ids:
            report["limitations"].append(
                "Multiple session UUIDs in launch arguments. Pass --session-id explicitly; the collector will not guess flag precedence."
            )
    project_dir = config / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    candidates = [
        {"id": p.stem, **file_stat(p)}
        for p in project_dir.glob("*.jsonl")
        if UUID.fullmatch(p.stem)
    ]
    report["session_candidates"] = sorted(
        candidates, key=lambda p: p.get("mtime_ns", 0), reverse=True
    )[:20]
    report["session_id"] = session_id
    report["logs"] = []
    report["subagents"] = []
    if session_id:
        for filename in (
            "claude-smart-routing-decisions.jsonl",
            "claude-smart-routing-audit.jsonl",
        ):
            report["logs"].append(log_events(home / ".ucode" / filename, session_id))
        # Search this session ID only, not other users' conversations.
        transcripts = list((config / "projects").glob(f"*/{session_id}.jsonl"))
        for path in transcripts:
            report["logs"].append(log_events(path, session_id, transcript=True))
            report["subagents"].append(subagent_evidence(path))
        if not transcripts:
            report["limitations"].append("Selected session transcript was not found.")
    else:
        report["limitations"].append(
            "Session ID unavailable. Pass --session-id to correlate logs; the global canary cannot identify this process reliably."
        )
    meta, canary = read_json(home / ".ucode/claude-smart-routing-canary.json")
    report["global_canary"] = {
        **meta,
        **{
            k: canary[k]
            for k in ("session_id", "model", "at")
            if isinstance(canary.get(k), (str, int, float))
        },
        "note": "Last global SessionStart, potentially from another process.",
    }
    return report


def summary(report: dict) -> str:
    lines = [
        "Claude routing snapshot",
        f"Collected: {report['collected_at']}",
        f"Selected PID: {report.get('process', {}).get('pid', 'none')}",
        f"Session: {report.get('session_id') or 'unknown'}",
    ]
    if report.get("process"):
        lines.append(f"Process inspection: {report['process'].get('status', 'unknown')}")
    for finding in report["findings"]:
        lines.append(f"{finding['check']}: {finding['status']}")
        for name in finding.get("missing_agents", []):
            lines.append(f"  Missing supplied definition: {name}")
    errors = [
        event
        for log in report["logs"]
        for event in log["events"]
        if event.get("event") == "agent_type_not_found"
    ]
    errors.extend(
        event
        for group in report.get("subagents", [])
        for agent in group["agents"]
        for event in agent["tool_evidence"]["events"]
        if event.get("event") == "agent_type_not_found"
    )
    lines.append(f"Actual missing-agent tool errors found in collected evidence: {len(errors)}")
    lines.extend(f"Limitation: {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="PID of the affected Claude process")
    parser.add_argument("--session-id", help="Affected Claude session UUID")
    parser.add_argument(
        "--output", type=Path, help="New report directory (default: unique directory in cwd)"
    )
    args = parser.parse_args()
    if args.pid is not None and args.pid <= 0:
        parser.error("--pid must be positive")
    if args.session_id and not UUID.fullmatch(args.session_id):
        parser.error("--session-id must be a UUID")
    report = collect(args.pid, args.session_id)
    # No prompts, arbitrary command strings, or environment dumps enter the report.
    serialized = json.dumps(report, indent=2).replace(str(Path.home()), "$HOME")
    report = json.loads(serialized)
    try:
        if args.output:
            args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
            output = args.output
        else:
            output = Path(tempfile.mkdtemp(prefix="claude-routing-diagnostics-", dir=Path.cwd()))
        for name, content in (("report.json", serialized + "\n"), ("summary.txt", summary(report))):
            with (output / name).open("x", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
    except OSError as exc:
        print(
            f"Cannot create report ({type(exc).__name__}); choose a new writable --output directory.",
            file=sys.stderr,
        )
        return 1
    print(summary(report), end="")
    print(f"Report: {output / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
