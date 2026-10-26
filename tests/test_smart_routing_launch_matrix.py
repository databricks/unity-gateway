"""Pin the smart routing outcome of every launch entry point, agent, and routing input.

Each row of ``fixtures/smart_routing_launch_matrix.tsv`` is one launch through the real CLI, with
auth, model discovery, config writing, and the agent process stubbed. After an intended behavior
change, regenerate the table and review its diff:

    UPDATE_SMART_ROUTING_LAUNCH_MATRIX=1 uv run pytest tests/test_smart_routing_launch_matrix.py
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from unittest.mock import patch

from tests.test_cli import _launch_policy_patches, runner
from ucode import cli as cli_mod
from ucode.constants import (
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
)
from ucode.smart_routing import config as smart_routing_config
from ucode.smart_routing import v2 as smart_routing_v2

MATRIX_PATH = Path(__file__).parent / "fixtures" / "smart_routing_launch_matrix.tsv"
UPDATE_ENV_VAR = "UPDATE_SMART_ROUTING_LAUNCH_MATRIX"

V2 = ENABLE_SMART_ROUTING_ENV_VAR
SUB = ENABLE_SUBAGENT_ROUTING_ENV_VAR
ORCH = ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR
VERSION = SMART_ROUTER_CONFIG_VERSION_ENV_VAR
ROUTER_NAME = "SMART_ROUTER_NAME"
ROUTING_KEYS = (V2, SUB, ORCH, VERSION)

# Isaac sets the orchestrator switch and router name from SAFE, plus a version for its cohort.
ISAAC_ENV = {ORCH: "1", ROUTER_NAME: "r9296-quality-20261008"}
ENV_CASES = {
    "none": {},
    "V2=1": {V2: "1"},
    "V2=0": {V2: "0"},
    "SUB=0": {SUB: "0"},
    "ORCH=1": {ORCH: "1"},
    "V2=0 SUB=1 ORCH=1": {V2: "0", SUB: "1", ORCH: "1"},
    "version first_prompt": {VERSION: smart_routing_config.FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0},
    "version subagent_only_v0": {VERSION: smart_routing_config.SUBAGENT_ONLY_V0},
    "version subagent_orch_v0": {VERSION: smart_routing_config.SUBAGENT_ORCH_V0},
    "version unknown": {VERSION: "not_a_version"},
    "isaac cohort": {VERSION: smart_routing_config.SUBAGENT_ORCH_V0_CLAUDE_ONLY, **ISAAC_ENV},
    "isaac default": ISAAC_ENV,
}
LAUNCH_ARGS = {
    "claude": [
        [],
        ["--", "fix the test"],
        ["-p", "fix the test"],
        ["update"],
        ["--model", "databricks-claude-sonnet-4"],
        ["--provider", "svc"],
    ],
    "codex": [[], ["--", "fix the test"], ["app"], ["--model", "gpt-5.5"], ["--provider", "svc"]],
    "gemini": [[]],
}
FLAGS = {
    "claude": [[], ["--enable-smart-routing"], ["--disable-smart-routing"]],
    "codex": [[], ["--enable-smart-routing"], ["--disable-smart-routing"]],
    "gemini": [[]],
}
COLUMNS = (
    "command",
    "env",
    "admin smart routing",
    "result",
    "routing",
    "V2/SUB/ORCH at launch",
    "version at launch",
    "codex config written with routing",
    "hook note",
)
HOOK_NOTE = "trust the ug routing hooks"


def _routing_env() -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in ROUTING_KEYS}


def _switches() -> str:
    return "/".join(os.environ.get(key) or "-" for key in (V2, SUB, ORCH))


def _routing_mode(agent: str, routed: bool) -> str:
    if not routed:
        return "off"
    mode = (
        "first prompt, subagents"
        if smart_routing_v2.first_prompt_routing_enabled(agent=agent)
        else "subagents"
    )
    return f"{mode}, orchestrator" if os.environ.get(ORCH) == "1" else mode


def _command(argv: list[str]) -> str:
    return " ".join(["ug", *(f'"{arg}"' if " " in arg else arg for arg in argv)])


def _launch(argv: list[str], agent: str, admin: bool | None, env: dict[str, str]) -> list[str]:
    """Run one launch and describe it as a table row; ``admin`` None means no admin config."""
    managed = (
        None if admin is None else {"enabled_agents": {agent: {"smart_routing_enabled": admin}}}
    )
    observed: dict[str, str] = {}
    with patch.dict(os.environ, env):
        env_before = _routing_env()
        with (
            _launch_policy_patches(managed) as calls,
            # Print mode moves ug's status output to stderr, which closes CliRunner's stream.
            patch("ucode.cli.redirect_output_to_stderr"),
            # Bare `ug` reads the admin config itself to choose the agent.
            patch("ucode.cli.install_databricks_cli"),
            patch("ucode.cli.refresh_managed_config", return_value=(managed, False)),
        ):

            def configure(*_args, **_kwargs):
                if agent == "codex":
                    routing = smart_routing_v2.smart_routing_enabled(
                        None, default=False, agent=agent
                    )
                    observed["codex_config"] = "on" if routing else "off"
                return calls["state"]

            def launch(*_args, options, **_kwargs):
                observed["routing"] = _routing_mode(agent, options.launch_smart_routing)
                observed["switches"] = _switches()
                observed["version"] = os.environ.get(VERSION) or "-"

            calls["configure"].side_effect = configure
            calls["launch"].side_effect = launch
            result = runner.invoke(cli_mod.app, argv)
        assert _routing_env() == env_before, f"{_command(argv)} leaked routing env changes"
    # Rich wraps long lines at the runner's terminal width.
    output = " ".join(result.output.split())
    if result.exit_code == 0:
        outcome = "launched"
    elif "smart routing cannot be enabled with" in output:
        outcome = "error: smart routing with a provider"
    else:
        outcome = f"error: exit {result.exit_code}"
    return [
        outcome,
        observed.get("routing", "-"),
        observed.get("switches", "-"),
        observed.get("version", "-"),
        observed.get("codex_config", "-"),
        "yes" if HOOK_NOTE in output else "no",
    ]


def _matrix_rows() -> list[list[str]]:
    rows = []
    for env_name, env in ENV_CASES.items():
        for agent, launch_args in LAUNCH_ARGS.items():
            for args in launch_args:
                for flag in FLAGS[agent]:
                    # An admin config rejects an explicit --provider before routing is considered.
                    admin_states = (None,) if "--provider" in args else (None, True)
                    for admin in admin_states:
                        argv = [agent, *flag, *args]
                        rows.append(
                            [
                                _command(argv),
                                env_name,
                                "no admin config" if admin is None else "on",
                                *_launch(argv, agent, admin, env),
                            ]
                        )
        # Bare `ug` launches the admin config's agent, so it always has an admin config.
        for agent in ("claude", "codex"):
            for admin in (False, True):
                rows.append(
                    [
                        f"ug (admin agent {agent})",
                        env_name,
                        "on" if admin else "off",
                        *_launch([], agent, admin, env),
                    ]
                )
    return rows


def test_smart_routing_launch_matrix(monkeypatch):
    for key in (*ROUTING_KEYS, ROUTER_NAME):
        monkeypatch.delenv(key, raising=False)
    actual = "".join("\t".join(row) + "\n" for row in [list(COLUMNS), *_matrix_rows()])

    if os.environ.get(UPDATE_ENV_VAR) == "1":
        MATRIX_PATH.write_text(actual)
    expected = MATRIX_PATH.read_text()

    diff = list(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            "expected (fixtures/smart_routing_launch_matrix.tsv)",
            "actual",
            lineterm="",
        )
    )
    assert not diff, (
        "Smart routing launch behavior changed. If intended, rerun with "
        f"{UPDATE_ENV_VAR}=1 and review the table diff:\n" + "\n".join(diff[:200])
    )
