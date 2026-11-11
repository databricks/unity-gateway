import json
import os
import runpy
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from controls import matches_labels

from ucode.os_compatibility import subprocess_cross_os as process

inspect_sessions = runpy.run_path(str(Path(__file__).parents[1] / "session-evidence.py"))[
    "inspect_sessions"
]
PLUGIN_ID = "cowork-probe@cowork-probe-marketplace"


def session_root():
    return Path.home() / "Library/Application Support/Claude-3p/local-agent-mode-sessions"


def find_plugins(payload):
    if isinstance(payload, list):
        return [plugin for item in payload for plugin in find_plugins(item)]
    if not isinstance(payload, dict):
        return []
    name, marketplace = PLUGIN_ID.split("@")
    if payload.get("pluginId", payload.get("id")) == PLUGIN_ID or (
        payload.get("name"),
        payload.get("marketplace"),
    ) == (name, marketplace):
        return [payload]
    return [plugin for item in payload.values() for plugin in find_plugins(item)]


def prepare_task(capability, desktop):
    task = {
        "capability": capability,
        "platform": "macos",
        "startedAfter": datetime.now(UTC).isoformat(),
        "expected": f"{capability}-proof-{uuid.uuid4().hex[:12]}",
    }
    if capability == "file":
        folder = Path.home() / "Documents/CoworkProbe"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "input.txt"
        path.write_text(task["expected"])
        task.update(
            folder=str(folder),
            expectedFile=str(path),
            prompt="Use the Read tool to read input.txt in the selected local folder. "
            "Reply with only the value stored in the file. Omit line numbers added by the tool. "
            "Do not use Markdown, code fences, or any other text. Do not guess the value.",
        )
        return task

    marketplace = desktop.TEMP / "cowork-probe-marketplace"
    plugin = marketplace / "plugins/cowork-probe"
    skill = plugin / "skills/probe-summary"
    for directory in (
        marketplace / ".claude-plugin",
        plugin / ".claude-plugin",
        skill / "references",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (marketplace / ".claude-plugin/marketplace.json").write_text(
        json.dumps(
            {
                "name": "cowork-probe-marketplace",
                "owner": {"name": "Unity Gateway CI"},
                "plugins": [{"name": "cowork-probe", "source": "./plugins/cowork-probe"}],
            }
        )
    )
    (plugin / ".claude-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "cowork-probe",
                "version": "1.0.0",
                "author": {"name": "Unity Gateway CI"},
                "description": "Native Cowork skill test fixture",
            }
        )
    )
    (skill / "SKILL.md").write_text(
        "---\nname: probe-summary\n"
        "description: Use when asked to produce the capability probe summary.\n---\n"
        "Use the Read tool to read references/facts.txt beside this skill. "
        "Reply with only the value stored in the file. Omit line numbers added by the tool. "
        "Do not use Markdown, code fences, or any other text.\n"
        "Do not guess the contents and do not use subagents.\n"
    )
    (skill / "references/facts.txt").write_text(task["expected"])
    accounts = list(session_root().glob("*/*/cowork_account_settings.json"))
    if len(accounts) != 1:
        raise RuntimeError("Expected one native Cowork account after baseline inference")
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("Pinned Claude CLI is missing")
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(accounts[0].parent)}
    commands = (
        ["plugin", "validate", str(marketplace)],
        ["plugin", "marketplace", "add", str(marketplace), "--cowork", "--json"],
        ["plugin", "install", PLUGIN_ID, "--cowork", "--json"],
        ["plugin", "list", "--cowork", "--json"],
    )
    for index, args in enumerate(commands):
        result = process.run(
            [claude, *args],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        desktop.save(f"skill-install-{index}.txt", result.stdout + "\n" + result.stderr)
        if result.returncode:
            raise RuntimeError("Native Cowork plugin command failed; see skill-install logs")
        if "--json" in args:
            payload = json.loads(result.stdout)
            if isinstance(payload, dict) and payload.get("outcome") not in (None, "ok", "success"):
                raise RuntimeError("Native Cowork plugin command reported failure")
    installed = [
        entry
        for entry in find_plugins(payload)
        if entry.get("enabled") is True and entry.get("installPath")
    ]
    if len(installed) != 1:
        raise RuntimeError("Expected one enabled native skill plugin")
    task.update(
        expectedFile=str(
            Path(installed[0]["installPath"]) / "skills/probe-summary/references/facts.txt"
        ),
        skill="cowork-probe:probe-summary",
        prompt="Use the probe-summary skill from the cowork-probe plugin to produce the "
        "capability probe summary. Reply with only its result. Do not use subagents.",
    )
    return task


def click_first(desktop, labels):
    snapshot = desktop.wait_for(labels, enabled=True)
    label = next(label for label in labels if matches_labels(snapshot, label, enabled=True))
    desktop.ui("click", label)


def new_task(desktop):
    labels = ("New", "New task", "New Task")
    snapshot = desktop.wait_for((*labels, "Show sidebar"), enabled=True)
    if not matches_labels(snapshot, labels, enabled=True):
        desktop.ui("click", "Show sidebar")
    click_first(desktop, labels)
    return desktop.open_cowork()


def select_folder(desktop, task):
    click_first(desktop, ("Project or folder", "Work in a folder", "Choose a folder", "Add folder"))
    picker = desktop.wait_for("Add folders to this session")
    desktop.save("folder-picker.json", picker)
    desktop.ui("key", "go-to-folder")
    desktop.wait_for(("Go to the folder:", "Go to Folder", "Go to folder:"))
    desktop.ui("folder-path", task["folder"])
    desktop.wait_for((task["folder"], "Go to:"), require_all=True)
    desktop.ui("key", "return")
    click_first(desktop, ("Select Folder", "Choose", "Open"))
    desktop.save("folder-permission.json", desktop.ui("snapshot"))
    desktop.wait_for(
        (
            "Allow Claude to change files in “CoworkProbe”?",
            "Allow Claude to access “CoworkProbe”?",
            "Allow Claude to work in “CoworkProbe”?",
        )
    )
    desktop.ui("click", "Allow")
    desktop.save("folder-selected.json", desktop.ui("snapshot"))


def runtime_task(task, root):
    if task["capability"] != "skill":
        return task
    source = Path(task["expectedFile"])
    references = root.glob("*/*/skills/probe-summary/references/facts.txt")
    copies = [
        path
        for path in references
        if path.read_bytes() == source.read_bytes()
        and (path.parents[1] / "SKILL.md").read_bytes()
        == (source.parents[1] / "SKILL.md").read_bytes()
    ]
    if len(copies) > 1:
        raise RuntimeError("Ambiguous native runtime copy of the test skill")
    return {**task, "expectedFile": str(copies[0])} if copies else task


def run_task(desktop, task, report):
    if task["capability"] == "file":
        select_folder(desktop, task)
        report["folderSelected"] = True
    desktop.ui("type", "Write your prompt to Claude", task["prompt"])
    desktop.ui("click", "Start task")
    report["submitted"] = True
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        runtime = runtime_task(task, Path(tempfile.gettempdir()) / "claude-hostloop-plugins")
        result = inspect_sessions(session_root(), runtime)
        if result["status"] == "pending":
            time.sleep(3)
            continue
        report["session"] = result
        if result["status"] != "passed":
            raise RuntimeError(result["error"])
        report.update(passed=True, responseAsserted=True)
        desktop.save("session.jsonl", Path(result["transcript"]).read_text())
        desktop.save("response-ui.json", desktop.ui("snapshot"))
        return
    raise RuntimeError("No completed native task with successful tool evidence within 300 seconds")


def capture_sessions(desktop, task):
    started = datetime.fromisoformat(task["startedAfter"]).timestamp()
    for index, path in enumerate(session_root().glob("**/*.jsonl")):
        try:
            if path.stat().st_mtime < started:
                continue
            text = path.read_text()
        except FileNotFoundError:
            continue
        if task["prompt"] in text:
            desktop.save(f"native-session-{index}.jsonl", text)
