"""Launch Omnigent, which owns its onboarding and reads UG's saved state."""

from __future__ import annotations

import shutil

import typer

from ucode.launcher import exec_or_spawn
from ucode.ui import print_err


def launch(tool_args: list[str]) -> None:
    """Forward arguments unchanged and hand over the controlling terminal."""
    executable = shutil.which("omnigent")
    if executable is None:
        print_err(
            "Omnigent is not installed. Run: uv tool install 'omnigent[databricks]' "
            "then retry ug omni."
        )
        raise typer.Exit(1)
    try:
        exec_or_spawn([executable, *tool_args])
    except OSError as exc:
        print_err(f"Could not launch Omnigent: {exc}. Check the Omnigent installation.")
        raise typer.Exit(1) from exc
