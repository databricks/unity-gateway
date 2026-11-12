"""MDM flow: an admin provisions the machine as root, then a user without sudo launches agents.

Stages run in file order: provision, machine-wide files, demote, launch Claude, launch Codex.
"""

import json
import re
import stat
import tomllib
from pathlib import Path

import pytest

from .conftest import (
    CLAUDE_PLUGINS,
    CLAUDE_SETTINGS,
    CODEX_SETTINGS,
    DARWIN,
    HOME,
    MACHINE_WIDE_ROOTS,
    POLICY,
    machine_snapshot,
    snapshot,
    user_uid,
)

PROMPT = "What is 6 times 7? Reply with only the number."
ANSWER = "42"
HEADER_MOD_FILES = (
    ".claude-plugin/marketplace.json",
    "ug-headers/.claude-plugin/plugin.json",
    "ug-headers/hooks/hooks.json",
    "ug-headers/hooks/register.ts",
)
USER_FILES = {
    "claude": (".ucode/state.json", ".ucode/refresh_ts.json", ".claude/ucode-settings.json"),
    "codex": (
        ".ucode/state.json",
        ".ucode/refresh_ts.json",
        ".codex/ucode.config.toml",
        ".ucode/codex-model-catalog.json",
    ),
}
LAUNCH = {
    "claude": ["ug", "claude", "-p", PROMPT, "--max-turns", "1"],
    "codex": ["ug", "codex", "exec", "--skip-git-repo-check", PROMPT],
}
# The aliases the macOS script adds to every user's shell startup files.
ALIASES = {
    "claude": "UG_SKIP_OS_SETTINGS=1 ug claude",
    "codex": "UG_SKIP_OS_SETTINGS=1 ug codex",
}
SHELL_RCS = (Path("/etc/zshrc"), Path("/etc/bashrc"))
# A Databricks OAuth token, such as the one the provisioning script mints and exports.
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def assert_root_owned(path, mode=0o644):
    st = path.stat()
    assert st.st_uid == 0 and stat.S_IMODE(st.st_mode) == mode, (
        f"{path}: {st.st_uid} {oct(st.st_mode)}"
    )


def test_provisioning_runs_without_a_password(machine, provisioned):
    assert provisioned.returncode == 0, provisioned.output[-4000:]
    assert "password" not in provisioned.output.lower(), provisioned.output[-4000:]
    # Configure exits 0 even when it skips an agent, so read its summary.
    assert "Failed to configure" not in provisioned.output, provisioned.output[-4000:]
    assert "Coding Agents:" in provisioned.output
    assert "Claude Code" in provisioned.output and "Codex" in provisioned.output


def test_workspace_policy_installed(machine, provisioned):
    for directory in (POLICY.parent, CLAUDE_SETTINGS.parent, CODEX_SETTINGS.parent):
        assert_root_owned(directory, 0o755)
    assert_root_owned(POLICY)
    policy = json.loads(POLICY.read_text())
    assert set(policy) == {"workspace", "config"}
    assert policy["workspace"].rstrip("/") == machine.workspace
    agents = {entry["agent"] for entry in policy["config"]["enabled_agents"]}
    assert {"CODING_AGENT_CLAUDE_CODE", "CODING_AGENT_CODEX"} <= agents


def test_claude_machine_wide_settings(machine, provisioned):
    assert_root_owned(CLAUDE_SETTINGS)
    settings = json.loads(CLAUDE_SETTINGS.read_text())
    helper = settings["apiKeyHelper"]
    assert helper.startswith("/usr/local/bin/ug auth-token"), helper
    assert machine.workspace in helper
    assert settings["env"]["ANTHROPIC_BASE_URL"] == f"{machine.workspace}/ai-gateway/anthropic"
    assert settings["availableModels"]
    marketplace = settings["extraKnownMarketplaces"]["ucode-managed"]["source"]
    assert marketplace == {"source": "directory", "path": str(CLAUDE_PLUGINS)}
    assert settings["enabledPlugins"] == {"ug-headers@ucode-managed": True}
    for name in HEADER_MOD_FILES:
        assert_root_owned(CLAUDE_PLUGINS / name)


def test_codex_machine_wide_config(machine, provisioned):
    assert_root_owned(CODEX_SETTINGS)
    config = tomllib.loads(CODEX_SETTINGS.read_text())
    provider = config["model_providers"][config["model_provider"]]
    assert provider["base_url"] == f"{machine.workspace}/ai-gateway/codex/v1"
    assert provider["auth"]["command"] == "/usr/local/bin/ug"
    assert config["model"]


def test_no_credentials_written_machine_wide(machine, provisioned):
    leaked = []
    for root in MACHINE_WIDE_ROOTS:
        for path in root.rglob("*") if root.exists() else ():
            if path.is_file():
                text = path.read_text(errors="ignore")
                if machine.client_secret in text or JWT.search(text):
                    leaked.append(str(path))
    assert not leaked, leaked


def test_provisioning_left_the_user_home_untouched(demoted):
    leaked = [p for p in demoted.home if p.startswith(f"{HOME}/.ucode") or ".claude" in p]
    assert not leaked, leaked


def test_user_cannot_sudo_after_demotion(machine, demoted):
    result = machine.as_user(["sudo", "-n", "true"])
    assert result.returncode != 0


@pytest.mark.skipif(not DARWIN, reason="Only the macOS script adds shell aliases")
def test_shell_aliases_installed(machine, demoted):
    rcs = [rc for rc in SHELL_RCS if rc.exists()]
    assert Path("/etc/zshrc") in rcs, rcs
    for rc in rcs:
        st = rc.stat()
        assert st.st_uid == 0 and not st.st_mode & 0o022, f"{rc}: {st.st_uid} {oct(st.st_mode)}"
        text = rc.read_text()
        assert text.count("# BEGIN unity-gateway") == 1, text
        assert text.count("# END unity-gateway") == 1, text

    # A login shell, as Terminal opens, run as the developer without sudo.
    for shell in ("zsh", "bash"):
        result = machine.as_user([shell, "-l", "-i", "-c", "alias claude codex"])
        assert result.returncode == 0, result.output[-4000:]
        for name, command in ALIASES.items():
            assert f"{name}='{command}'" in result.output, (shell, result.output[-4000:])


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_demoted_user_launches_agent(machine, demoted, agent):
    result = machine.as_user(
        LAUNCH[agent],
        timeout=240,
        UG_SKIP_OS_SETTINGS="1",
        DATABRICKS_BEARER=demoted.bearer,
    )
    assert result.returncode == 0, result.output[-4000:]
    assert ANSWER in result.output, result.output[-4000:]
    assert "password" not in result.output.lower(), result.output[-4000:]

    assert machine_snapshot() == demoted.machine_wide

    home = snapshot([(HOME, False)])
    foreign = {p: row[1] for p, row in home.items() if row[1] != user_uid()}
    assert not foreign, foreign
    for name in USER_FILES[agent]:
        assert f"{HOME}/{name}" in home, name
    # The installed policy supplies the workspace, so no per-user managed config is written.
    assert f"{HOME}/.ucode/managed-config.json" not in home
