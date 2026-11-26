"""Isaac feedback commands used by the native Codex launcher."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from ucode.constants import (
    ISAAC_LAUNCH_MODE_ENV_VAR,
    ISAAC_OMNI_LAUNCH_MODE,
    ISAAC_SMART_ROUTER_FEEDBACK_ENV_VAR,
)
from ucode.env import env_bool
from ucode.os_compatibility import subprocess_cross_os
from ucode.smart_routing.session_env import SESSION_ENV_VAR, effective_environment


def smart_router_feedback_required() -> bool:
    return (
        env_bool(ISAAC_SMART_ROUTER_FEEDBACK_ENV_VAR)
        and os.environ.get(ISAAC_LAUNCH_MODE_ENV_VAR) != ISAAC_OMNI_LAUNCH_MODE
    )


def _isaac() -> str:
    executable = shutil.which("isaac")
    if not executable:
        raise RuntimeError("Isaac feedback is unavailable. Routing is unchanged.")
    return executable


def prepare_codex_feedback(env: Mapping[str, str]) -> str | None:
    try:
        result = subprocess_cross_os.run(
            [
                _isaac(),
                "feedback",
                "--inspect-target",
                "--harness",
                "codex",
                "--session-id",
                env["CODEX_THREAD_ID"],
            ],
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            raise ValueError("Could not identify the feedback target.")
        target = json.loads(result.stdout)
        if not isinstance(target, dict) or target.get("session_id") != env["CODEX_THREAD_ID"]:
            raise ValueError("Invalid native feedback session.")
        turn_id = target.get("turn_id") or target.get("message_id")
        if turn_id is not None and not isinstance(turn_id, str):
            raise ValueError("Invalid native feedback task.")
        return turn_id
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Could not prepare feedback. Smart Router is still on.") from exc


class FeedbackUpload(Protocol):
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


def submit_codex_feedback(
    feedback_id: str,
    env: Mapping[str, str],
    *,
    reason: str,
    turn_id: str | None,
) -> FeedbackUpload:
    report_id = str(UUID(feedback_id))
    args = [
        _isaac(),
        "feedback",
        "--category",
        "smart-routing",
        "--harness",
        "codex",
        "--session-id",
        env["CODEX_THREAD_ID"],
        "--feedback-id",
        report_id,
        "--with-evidence",
        "--turn-id",
        turn_id or "no-prior-task",
    ]
    environment = effective_environment(env, agent="codex", strict=True)
    environment[SESSION_ENV_VAR] = ""
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdin:
        stdin.write(reason)
        stdin.seek(0)
        return subprocess_cross_os.popen(
            args,
            stdin=stdin,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            **options,
        )


async def wait_for_feedback(process: FeedbackUpload, timeout: float = 120) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while (exit_code := process.poll()) is None:
                await asyncio.sleep(0.1)
        return exit_code == 0
    except TimeoutError:
        with contextlib.suppress(OSError):
            process.kill()
        await asyncio.to_thread(process.wait)
        return False
