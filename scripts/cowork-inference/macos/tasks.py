import json
import os
import runpy
import shutil
import subprocess
import sys
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
FIXTURE = json.loads((Path(__file__).parents[1] / "task-fixture.json").read_text())
PLUGIN_ID = FIXTURE["pluginId"]
# Desktop may ask before a connector tool's first use; the prompt label is not pinned.
TOOL_PERMISSION_LABELS = (
    "Allow once",
    "Allow for this task",
    "Allow for this chat",
    "Always allow",
)


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
            prompt=FIXTURE["prompts"]["file"],
        )
        return task
    if capability == "mcp":
        return prepare_mcp_task(task, desktop)

    marketplace = desktop.TEMP / FIXTURE["marketplace"]["name"]
    skill = marketplace / FIXTURE["skillDirectory"]
    plugin = skill.parents[1]
    for directory in (
        marketplace / ".claude-plugin",
        plugin / ".claude-plugin",
        skill / "references",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (marketplace / ".claude-plugin/marketplace.json").write_text(json.dumps(FIXTURE["marketplace"]))
    (plugin / ".claude-plugin/plugin.json").write_text(json.dumps(FIXTURE["plugin"]))
    (skill / "SKILL.md").write_text(FIXTURE["skillInstructions"])
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
        skill=FIXTURE["skill"],
        prompt=FIXTURE["prompts"]["skill"],
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


def keyboard(desktop, action, text=""):
    script = """on run argv
    tell application "System Events"
        set frontmost of (first process whose unix id is (item 1 of argv as integer)) to true
        if item 2 of argv is "go-to-folder" then
            keystroke "g" using {command down, shift down}
        else if item 2 of argv is "path" then
            keystroke (item 3 of argv)
        else if item 2 of argv is "return" then
            key code 36
        end if
    end tell
end run"""
    result = process.run(
        ["/usr/bin/osascript", "-e", script, desktop.ui("pid").strip(), action, text],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"Native keyboard input failed: {result.stderr.strip()}")


def select_folder(desktop, task):
    click_first(desktop, ("Project or folder", "Work in a folder", "Choose a folder", "Add folder"))
    picker = desktop.wait_for("Add folders to this session")
    desktop.save("folder-picker.json", picker)
    keyboard(desktop, "go-to-folder")
    desktop.wait_for(("Go to the folder:", "Go to Folder", "Go to folder:"))
    keyboard(desktop, "path", task["folder"])
    desktop.wait_for((task["folder"], "Go to:"), require_all=True)
    keyboard(desktop, "return")
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


def matches_installed_skill(copy, source):
    try:
        return (
            copy.read_bytes() == source.read_bytes()
            and (copy.parents[1] / "SKILL.md").read_bytes()
            == (source.parents[1] / "SKILL.md").read_bytes()
        )
    except FileNotFoundError:  # Desktop may still be copying the plugin.
        return False


def prepare_mcp_task(task, desktop):
    profile_path = (
        Path.home() / "Library/Application Support/Claude-3p/configLibrary/"
        "5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json"
    )
    profile = json.loads(profile_path.read_text())
    servers = [
        server
        for server in profile.get("managedMcpServers", [])
        if server.get("name") == FIXTURE["mcpServer"]
    ]
    if len(servers) != 1:
        raise RuntimeError(f"Desktop profile is missing the {FIXTURE['mcpServer']} connector")
    arguments = servers[0].get("args", [])
    expected_url = (
        os.environ["UCODE_TEST_WORKSPACE"].rstrip("/")
        + "/ai-gateway/mcp-services/"
        + FIXTURE["mcpService"]
    )
    if (
        servers[0].get("transport") != "stdio"
        or "--url" not in arguments
        or arguments[arguments.index("--url") + 1] != expected_url
    ):
        raise RuntimeError(
            f"The {FIXTURE['mcpServer']} connector does not proxy {FIXTURE['mcpService']}"
        )
    # Diagnostic only: shows whether the connector starts outside Desktop and with Desktop's reduced env.
    check = process.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "mcp-connect-check.py"),
            "--profile",
            str(profile_path),
            "--server",
            FIXTURE["mcpServer"],
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    desktop.save("mcp-connect.json", check.stdout + check.stderr)
    task.update(
        mcpServer=FIXTURE["mcpServer"],
        mcpTool=FIXTURE["mcpTool"],
        prompt=FIXTURE["prompts"]["mcp"],
    )
    return task


def approve_tool_permission(desktop, report):
    try:
        snapshot = desktop.ui("snapshot")
    except (RuntimeError, subprocess.TimeoutExpired):
        return  # Accessibility snapshots can time out; the next poll retries.
    for label in TOOL_PERMISSION_LABELS:
        if matches_labels(snapshot, label, enabled=True):
            desktop.ui("click", label, wait_until_enabled=False)
            report["toolPermission"] = label
            return


def runtime_task(task, root):
    """Return the task bound to Desktop's runtime skill copy, or None until it exists."""
    if task["capability"] != "skill":
        return task
    source = Path(task["expectedFile"])
    references = root.glob("*/*/skills/probe-summary/references/facts.txt")
    copies = [path for path in references if matches_installed_skill(path, source)]
    if len(copies) > 1:
        raise RuntimeError("Ambiguous native runtime copy of the test skill")
    return {**task, "expectedFile": str(copies[0])} if copies else None


def run_task(desktop, task, report):
    if task["capability"] == "file":
        select_folder(desktop, task)
        report["folderSelected"] = True
    desktop.ui("type", "Write your prompt to Claude", task["prompt"])
    desktop.ui("click", "Start task")
    report["submitted"] = True
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if task["capability"] == "mcp":
            approve_tool_permission(desktop, report)
        runtime = runtime_task(task, Path(tempfile.gettempdir()) / "claude-hostloop-plugins")
        if task["capability"] == "skill":
            report["runtimeSkillCopyFound"] = runtime is not None
        result = inspect_sessions(session_root(), runtime) if runtime else {"status": "pending"}
        if result["status"] == "pending":
            time.sleep(3)
            continue
        report["session"] = result
        if result["status"] != "passed":
            raise RuntimeError(result["error"])
        report.update(passed=True, responseAsserted=True)
        desktop.save(
            "session.jsonl",
            Path(result["transcript"]).read_text(encoding="utf-8", errors="replace"),
        )
        desktop.save("response-ui.json", desktop.ui("snapshot"))
        return
    raise RuntimeError("No completed native task with successful tool evidence within 300 seconds")


def capture_sessions(desktop, task):
    started = datetime.fromisoformat(task["startedAfter"]).timestamp()
    for index, path in enumerate(session_root().glob("**/*.jsonl")):
        try:
            if path.stat().st_mtime < started:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        if task["prompt"] in text:
            desktop.save(f"native-session-{index}.jsonl", text)
    if task["capability"] == "mcp":
        capture_desktop_logs(desktop, started)


DESKTOP_LOG_TAIL_BYTES = 200_000


def capture_desktop_logs(desktop, started):
    """Save Desktop's logs and UG's MCP proxy backend log, which explain a closed connector."""
    paths = sorted((Path.home() / "Library/Logs").glob("Claude*/**/*.log"))
    paths += sorted((Path.home() / ".ucode/mcp-proxy").glob("*/backend.log"))
    for path in paths:
        try:
            if path.stat().st_mtime < started:
                continue
            data = path.read_bytes()[-DESKTOP_LOG_TAIL_BYTES:]
        except FileNotFoundError:
            continue
        name = "-".join(path.relative_to(Path.home()).parts)
        desktop.save(f"desktop-log-{name}", data.decode("utf-8", errors="replace"))
