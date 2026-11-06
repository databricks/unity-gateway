"""Run the real Codex TUI with opt-in reload on Smart Router toggles."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from ucode.agents.codex import render_overlay
from ucode.smart_routing import v2
from ucode.smart_routing.config import apply_enabled_version
from ucode.state import load_state
from ucode.workspace import resolve_workspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="Defaults to the configured UG workspace.")
    parser.add_argument("--profile", help="Databricks authentication profile.")
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--session-controls",
        type=Path,
        help="Reuse this session's routing state when recovering a saved conversation.",
    )
    parser.add_argument("codex_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    binary = shutil.which("codex")
    if binary is None:
        parser.error("Codex is not installed")
    state = load_state()
    if args.workspace:
        workspace = resolve_workspace(args.workspace)
        if workspace != state.get("workspace"):
            state = {"workspace": workspace}
    if not state.get("workspace"):
        parser.error("Configure UG first or supply --workspace and --profile")
    if args.profile:
        state["profile"] = args.profile
    if state.get("codex_relayed"):
        parser.error("This POC requires direct AI Gateway access, not subscription relay")
    apply_enabled_version(agent="codex")
    if not v2.smart_routing_enabled(None, default=False, agent="codex"):
        parser.error(
            "Select a Codex-enabled preset, e.g. SMART_ROUTER_CONFIG_VERSION=subagent_orch_v1"
        )
    tool_args = args.codex_args
    if tool_args[:1] == ["--"]:
        tool_args = tool_args[1:]
    v2.launch_codex(
        state,
        tool_args,
        binary=binary,
        start_model=args.model,
        render_overlay=render_overlay,
        reload_on_toggle=True,
        resume_controls=args.session_controls,
    )


if __name__ == "__main__":
    main()
