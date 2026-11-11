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
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.installed.node_install import nvm_node_install_snippet
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

INSTALL_DIR = PurePosixPath("/installed-agent")
UG = '"$HOME/.local/bin/ug"'
# archive.ubuntu.com can stall from GitHub runners; retry apt and use the runners' Azure mirror.
APT_SETUP = (
    "if [ -d /etc/apt/apt.conf.d ]; then "
    'printf \'Acquire::Retries "5";\\nAcquire::http::Timeout "30";\\n\' '
    "> /etc/apt/apt.conf.d/80-ug-bench; "
    "sed -i 's#http://archive.ubuntu.com/ubuntu#http://azure.archive.ubuntu.com/ubuntu#g' "
    "/etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true; fi"
)
SHELL_PREFIX = 'export PATH="$HOME/.local/bin:$PATH"; if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh"; fi; '


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set on the host running harbor")
    return value


class _UgAgent(BaseInstalledAgent):
    AGENT = ""
    PROMPT_ON_STDIN = False
    capabilities = AgentCapabilities(resume=True)

    async def _install_agent_cli(self, environment: BaseEnvironment) -> None:
        raise NotImplementedError

    def _launch_args(self, resume: bool) -> str:
        """Agent args after ug's `--`. Without PROMPT_ON_STDIN they pass $UG_BENCH_INSTRUCTION."""
        raise NotImplementedError

    async def install(self, environment: BaseEnvironment) -> None:
        wheel = Path(_required_env("UG_BENCH_WHEEL"))
        await self.exec_as_root(environment, command=APT_SETUP)
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
            "ENABLE_SMART_ROUTING_V2": "1",
            "UG_BENCH_INSTRUCTION": instruction,
        }
        if not self._resume:
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
        stdin = 'printf "%s" "$UG_BENCH_INSTRUCTION" | ' if self.PROMPT_ON_STDIN else ""
        try:
            await self.exec_as_agent(
                environment,
                command=(
                    f"{SHELL_PREFIX}{stdin}{UG} {self.AGENT} {model}-- {self._launch_args(self._resume)} "
                    f"2>&1 | tee -a {log.as_posix()}"
                ),
                env=env,
            )
        finally:
            # ug's routing wrappers log under ~/.ucode; keep them as evidence that routing ran.
            ucode_logs = (self.environment_logs_dir / "ucode").as_posix()
            await environment.exec(
                command=f'mkdir -p {ucode_logs} && cp "$HOME"/.ucode/*.log {ucode_logs}/ 2>/dev/null || true'
            )


class UgClaude(_UgAgent):
    AGENT = "claude"
    PROMPT_ON_STDIN = True

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

    def _launch_args(self, resume: bool) -> str:
        args = "-p --verbose --output-format stream-json --dangerously-skip-permissions"
        if not self.model_name:
            # A task's [environment] env can pin Claude's model, which skips Smart Router's root pick.
            args = '${UG_BENCH_CLAUDE_MODEL:+--model "$UG_BENCH_CLAUDE_MODEL"} ' + args
        return f"{args} --continue" if resume else args


class UgCodex(_UgAgent):
    AGENT = "codex"

    @staticmethod
    def name() -> str:
        return "ug-codex"

    async def _install_agent_cli(self, environment: BaseEnvironment) -> None:
        version = f"@{self._version}" if self._version else "@latest"
        # Harbor drops setup output on success; keep a timestamped copy to find slow steps.
        log = (self.environment_logs_dir / "codex-install.txt").as_posix()
        await self.exec_as_agent(
            environment,
            command=(
                f"set -euo pipefail; {{ {nvm_node_install_snippet()} && "
                f"npm install -g @openai/codex{shlex.quote(version)} && codex --version; }} 2>&1 | "
                f'while IFS= read -r line; do printf "%s %s\\n" "$(date +%T)" "$line"; done | '
                f"tee -a {log}"
            ),
            env={"NVM_NODEJS_ORG_MIRROR": "https://nodejs.org/dist"},
        )

    def _launch_args(self, resume: bool) -> str:
        return (
            f"exec {'resume --last ' if resume else ''}--dangerously-bypass-approvals-and-sandbox "
            '--skip-git-repo-check --json --enable unified_exec -- "$UG_BENCH_INSTRUCTION" </dev/null'
        )
