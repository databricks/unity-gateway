"""Harbor agents that run Claude Code and Codex inside a task container through ug.

Use with `harbor run -a ug_agent:UgClaude` (or `UgCodex`) and this directory on
PYTHONPATH. The host must export UG_BENCH_WHEEL (a built ug wheel) plus the
workspace auth that bench_auth.py reads.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path, PurePosixPath

import bench_auth
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.installed.node_install import nvm_node_install_snippet
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

INSTALL_DIR = PurePosixPath("/installed-agent")
UG = '"$HOME/.local/bin/ug"'
SHELL_PREFIX = 'export PATH="$HOME/.local/bin:$PATH"; if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh"; fi; '


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set on the host running harbor")
    return value


class _UgAgent(BaseInstalledAgent):
    AGENT = ""

    async def _install_agent_cli(self, environment: BaseEnvironment) -> None:
        raise NotImplementedError

    def _launch_args(self) -> str:
        raise NotImplementedError

    async def install(self, environment: BaseEnvironment) -> None:
        wheel = Path(_required_env("UG_BENCH_WHEEL"))
        await self.ensure_system_dependencies(
            environment, ("curl", "bash", "git", "ca_certificates", "procps", "unzip")
        )
        await environment.upload_file(wheel, str(INSTALL_DIR / wheel.name))
        await self.exec_as_root(
            environment,
            command=(
                "command -v databricks >/dev/null || curl -fsSL "
                "https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh"
            ),
        )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; curl -LsSf https://astral.sh/uv/install.sh | sh && "
                f'"$HOME/.local/bin/uv" tool install --python 3.12 {shlex.quote(str(INSTALL_DIR / wheel.name))}'
            ),
        )
        await self._install_agent_cli(environment)

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        auth = bench_auth.agent_env({})
        workspace = auth["DATABRICKS_HOST"]
        env = {
            **auth,
            # Lets Claude Code skip permissions as root inside the container.
            "IS_SANDBOX": "1",
            "UG_BENCH_INSTRUCTION": instruction,
        }
        await self.exec_as_agent(
            environment,
            command=(
                f"{SHELL_PREFIX}{UG} configure --agents {self.AGENT} "
                f"--workspace {shlex.quote(workspace)} --skip-validate --skip-upgrade "
                "--disable-databricks-ai-tools </dev/null"
            ),
            env=env,
        )
        model = f"--model {shlex.quote(self.model_name)} " if self.model_name else ""
        log = self.environment_logs_dir / f"ug-{self.AGENT}.txt"
        await self.exec_as_agent(
            environment,
            command=(
                f'{SHELL_PREFIX}printf "%s" "$UG_BENCH_INSTRUCTION" | '
                f"{UG} {self.AGENT} {model}-- {self._launch_args()} 2>&1 | tee {log.as_posix()}"
            ),
            env=env,
        )


class UgClaude(_UgAgent):
    AGENT = "claude"

    @staticmethod
    def name() -> str:
        return "ug-claude"

    async def _install_agent_cli(self, environment: BaseEnvironment) -> None:
        version = f" {shlex.quote(self._version)}" if self._version else ""
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; curl -fsSL "
                f"https://downloads.claude.ai/claude-code-releases/bootstrap.sh | bash -s --{version} && "
                '"$HOME/.local/bin/claude" --version'
            ),
        )

    def _launch_args(self) -> str:
        return "-p --verbose --output-format stream-json --dangerously-skip-permissions"


class UgCodex(_UgAgent):
    AGENT = "codex"

    @staticmethod
    def name() -> str:
        return "ug-codex"

    async def _install_agent_cli(self, environment: BaseEnvironment) -> None:
        version = f"@{self._version}" if self._version else "@latest"
        await self.exec_as_agent(
            environment,
            command=(
                f"set -euo pipefail; {nvm_node_install_snippet()} && "
                f"npm install -g @openai/codex{shlex.quote(version)} && codex --version"
            ),
            env={"NVM_NODEJS_ORG_MIRROR": "https://nodejs.org/dist"},
        )

    def _launch_args(self) -> str:
        return "exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --json -"
