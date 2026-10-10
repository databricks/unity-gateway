"""Bounded native Claude sessions for component tests (no gateway credentials)."""

import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ucode.constants import AGENT_CLAUDE
from ucode.smart_routing.config import resolve_environment
from ucode.smart_routing.v2 import (
    CLAUDE_ROUTING_PLUGIN_NAME,
    _claude_mod_launch_options,
    _write_routed_claude_plugin,
)


class Session:
    def __init__(self, binary, root, env, api="http://127.0.0.1:1"):
        self.root = root
        root.mkdir()
        self.plugin = root / "plugin"
        _write_routed_claude_plugin(self.plugin, [])
        shutil.copyfile(Path(__file__).with_name("observe.ts"), self.plugin / "hooks/observe.ts")
        (self.plugin / "hooks/hooks.json").write_text('{"modules":["./observe.ts"]}')
        skill = root / "config/skills/smart-router"
        skill.mkdir(parents=True)
        shutil.copyfile(
            Path(__file__).parents[2] / "skills/smart-router/SKILL.md", skill / "SKILL.md"
        )
        # Use an existing skill command, like production, so reload needs no registration.
        probe_skill = root / "config/skills/routing-probe"
        probe_skill.mkdir()
        (probe_skill / "SKILL.md").write_text(
            "---\nname: routing-probe\ndescription: Observe routing in component tests\n---\n"
            "The test mod handles this command locally.\n"
        )
        settings = {
            "env": {
                **resolve_environment(env, agent=AGENT_CLAUDE),
                "CLAUDE_CODE_EXTRA_BODY": '{"caller_field":"keep"}',
            },
            "pluginConfigs": {
                CLAUDE_ROUTING_PLUGIN_NAME: {"options": _claude_mod_launch_options(env)}
            },
        }
        probe = root / "hook.py"
        probe.write_text(
            "import json, os\nfrom pathlib import Path\n"
            "keys = ('ENABLE_SMART_ROUTING_V2', 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY', 'ENABLE_SMART_ROUTER_ORCHESTRATOR')\n"
            "Path('hook-env.json').write_text(json.dumps({k: os.environ.get(k) for k in keys}))\n"
            "print('{}')\n"
        )
        argv = [sys.executable, str(probe)]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        settings["hooks"] = {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]
        }
        process_env = {
            **{
                key: os.environ[key]
                for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
                if key in os.environ
            },
            "HOME": str(root),
            "CLAUDE_CONFIG_DIR": str(root / "config"),
            "ANTHROPIC_BASE_URL": api,
            "ANTHROPIC_API_KEY": "fixture-dummy-key",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        self.proc = subprocess.Popen(
            [
                binary,
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--setting-sources",
                "user",
                "--settings",
                json.dumps(settings),
                "--model",
                "claude-sonnet-4-6",
                "--tools",
                "Agent",
                "--allowedTools",
                "Agent",
                "--plugin-dir",
                str(self.plugin),
            ],
            cwd=root,
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.lines = queue.Queue()
        self.errors = []
        self.readers = [
            threading.Thread(
                target=lambda: [self.lines.put(line) for line in self.proc.stdout], daemon=True
            ),
            threading.Thread(target=lambda: self.errors.extend(self.proc.stderr), daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def send(self, prompt):
        self.proc.stdin.write(
            json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n"
        )
        self.proc.stdin.flush()
        deadline = time.monotonic() + 30
        while True:
            event = json.loads(self.lines.get(timeout=max(0, deadline - time.monotonic())))
            if event.get("type") == "result":
                assert not event.get("is_error"), event.get("result")
                return event

    def observe(self):
        result = self.send("/routing-probe")
        assert result["num_turns"] == 0 and result["usage"]["output_tokens"] == 0
        return json.loads(result["result"].partition(": ")[2])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()
