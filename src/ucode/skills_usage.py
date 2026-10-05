"""Report downloaded skills to AI Gateway, which counts them toward UC skill popularity.

Reports are sent by a detached child process, so no command waits on the network and neither an
exit nor an ``os.execvp`` into an agent cuts a report short.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from contextlib import suppress
from typing import IO, Any

from ucode.databricks import _http_post_json, workspace_hostname
from ucode.os_compatibility import subprocess_cross_os
from ucode.skills_api import SkillRef
from ucode.telemetry import ug_version

_REPORT_SKILL_USAGE_PATH = "/ai-gateway/skills:reportSkillUsage"
_MAX_SKILLS_PER_REPORT = 50
_REPORT_TIMEOUT_SECONDS = 10
_DETACHED_POPEN_OPTIONS: dict[str, Any] = (
    {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    if sys.platform == "win32"
    else {"start_new_session": True}
)


def report_skill_usage_in_background(workspace: str, token: str, refs: list[SkillRef]) -> None:
    """Hand ``refs`` to a detached reporter process and return without waiting for it.

    The request is written from a daemon thread, so one larger than the OS pipe buffer never
    holds up the command while the reporter starts.
    """
    skills = [{"full_name": ref.fqn, "id": ref.skill_id} for ref in refs if ref.skill_id]
    if not skills:
        return
    request = json.dumps({"workspace": workspace, "token": token, "skills": skills})
    with suppress(OSError):
        reporter = subprocess_cross_os.popen(
            [sys.executable, "-m", "ucode.skills_usage"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            **_DETACHED_POPEN_OPTIONS,
        )
        threading.Thread(target=_write_request, args=(reporter.stdin, request), daemon=True).start()


def _write_request(stdin: IO[str], request: str) -> None:
    with suppress(OSError), stdin:
        stdin.write(request)


def main() -> None:
    """Send the request read from stdin, one report per 50 skills, ignoring every failure."""
    request = json.load(sys.stdin)
    url = f"https://{workspace_hostname(request['workspace'])}{_REPORT_SKILL_USAGE_PATH}"
    headers = {"User-Agent": f"ucode/{ug_version()}"}
    token, skills = request["token"], request["skills"]
    for start in range(0, len(skills), _MAX_SKILLS_PER_REPORT):
        report = {"skills": skills[start : start + _MAX_SKILLS_PER_REPORT]}
        _http_post_json(url, token, report, timeout=_REPORT_TIMEOUT_SECONDS, headers=headers)


if __name__ == "__main__":
    main()
