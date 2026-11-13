"""Diagnose a Desktop MCP connector by starting its command the way Desktop would.

Runs the connector from Desktop's profile twice, with the full job environment and with only
the basic system variables an MCP client passes by default, and reports whether each answers an
MCP `initialize` request. Prints a JSON report; it never fails the task by itself.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ucode.os_compatibility import subprocess_cross_os as process

# The MCP SDK's default stdio environment, which desktop MCP clients start servers with.
DEFAULT_KEYS = (
    ("APPDATA", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH", "PROCESSOR_ARCHITECTURE")
    + ("SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "USERNAME", "USERPROFILE", "PROGRAMFILES")
    if os.name == "nt"
    else ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")
)
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "cowork-ci-check", "version": "1"},
    },
}


def check(server, environment):
    command = [server["command"], *server.get("args", [])]
    try:
        result = process.run(
            command,
            input=json.dumps(INITIALIZE) + "\n",
            capture_output=True,
            text=True,
            env={**environment, **(server.get("env") or {})},
            timeout=45,
        )
        returncode = result.returncode
        stdout, stderr = result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        # A healthy stdio server keeps running after answering; a timeout after output is fine.
        returncode = None
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
    return {
        "initialized": '"id": 1' in stdout.replace('"id":1', '"id": 1') and '"result"' in stdout,
        "returncode": returncode,
        "stdout": stdout[-2000:],
        "stderr": stderr[-4000:],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    server = next(s for s in profile["managedMcpServers"] if s.get("name") == args.server)
    minimal = {key: os.environ[key] for key in DEFAULT_KEYS if key in os.environ}
    print(
        json.dumps(
            {
                "command": [server["command"], *server.get("args", [])],
                "configuredEnvKeys": sorted(server.get("env") or {}),
                "fullEnvironment": check(server, dict(os.environ)),
                "defaultEnvironment": check(server, minimal),
            }
        )
    )


if __name__ == "__main__":
    sys.exit(main())
