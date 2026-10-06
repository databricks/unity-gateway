"""Report downloaded skills to AI Gateway, which counts them toward UC skill popularity.

Reports are sent by a detached child process, so no command waits on the network and exiting
doesn't cut a report short.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import suppress
from typing import Any

from ucode.databricks import _http_post_json, workspace_hostname
from ucode.os_compatibility import subprocess_cross_os
from ucode.skills_api import SkillRef
from ucode.telemetry import ug_version

_REPORT_SKILL_USAGE_PATH = "/ai-gateway/skills:reportSkillUsage"
_REPORT_REQUEST_ENV_VAR = "UCODE_SKILL_USAGE_REPORT"
_MAX_SKILLS_PER_REPORT = 50
_REPORT_TIMEOUT_SECONDS = 10
_DETACHED_POPEN_OPTIONS: dict[str, Any] = (
    {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    if sys.platform == "win32"
    else {"start_new_session": True}
)


def report_skill_usage_in_background(workspace: str, token: str, refs: list[SkillRef]) -> None:
    """Hand ``refs`` to a detached reporter process and return without waiting for it.

    The skills go in the reporter's environment and the token on its stdin, so the token stays
    out of the environment and the whole request is handed over before this returns. ``-P`` keeps
    a ``ucode`` package in the working directory from shadowing this one.
    """
    skills = [{"full_name": ref.fqn, "id": ref.skill_id} for ref in refs if ref.skill_id]
    if not skills:
        return
    request = json.dumps({"workspace": workspace, "skills": skills})
    with suppress(OSError):
        reporter = subprocess_cross_os.popen(
            [sys.executable, "-P", "-m", "ucode.skills_usage"],
            env={**os.environ, _REPORT_REQUEST_ENV_VAR: request},
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            **_DETACHED_POPEN_OPTIONS,
        )
        assert reporter.stdin is not None
        with reporter.stdin:
            reporter.stdin.write(token)


def main() -> None:
    """Send the handed over request, one report per 50 skills, stopping at the first failure."""
    token = sys.stdin.read()
    request = json.loads(os.environ[_REPORT_REQUEST_ENV_VAR])
    url = f"https://{workspace_hostname(request['workspace'])}{_REPORT_SKILL_USAGE_PATH}"
    headers = {"User-Agent": f"ucode/{ug_version()}"}
    skills = request["skills"]
    for start in range(0, len(skills), _MAX_SKILLS_PER_REPORT):
        report = {"skills": skills[start : start + _MAX_SKILLS_PER_REPORT]}
        _, reason = _http_post_json(
            url, token, report, timeout=_REPORT_TIMEOUT_SECONDS, headers=headers
        )
        if reason:
            return


if __name__ == "__main__":
    main()
