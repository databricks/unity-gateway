"""Report downloaded skills to AI Gateway, which counts them toward UC skill popularity."""

from __future__ import annotations

import threading

from ucode.databricks import _http_post_json, workspace_hostname
from ucode.skills_api import SkillRef
from ucode.telemetry import ug_version

_REPORT_SKILL_USAGE_PATH = "/ai-gateway/skills:reportSkillUsage"
_MAX_SKILLS_PER_REPORT = 50
_REPORT_TIMEOUT_SECONDS = 2


def report_skill_usage(workspace: str, token: str, refs: list[SkillRef]) -> None:
    """Send one report per 50 skills, ignoring every failure."""
    skills = [{"full_name": ref.fqn, "id": ref.skill_id} for ref in refs if ref.skill_id]
    url = f"https://{workspace_hostname(workspace)}{_REPORT_SKILL_USAGE_PATH}"
    headers = {"User-Agent": f"ucode/{ug_version()}"}
    for start in range(0, len(skills), _MAX_SKILLS_PER_REPORT):
        batch = skills[start : start + _MAX_SKILLS_PER_REPORT]
        _http_post_json(
            url, token, {"skills": batch}, timeout=_REPORT_TIMEOUT_SECONDS, headers=headers
        )


def report_skill_usage_in_background(workspace: str, token: str, refs: list[SkillRef]) -> None:
    """``report_skill_usage`` on a non-daemon thread, so the command's exit waits for it."""
    threading.Thread(
        target=report_skill_usage, args=(workspace, token, refs), name="skill-usage-report"
    ).start()
