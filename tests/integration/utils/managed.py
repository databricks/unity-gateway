"""Shared fixture loading and stub injection for managed-config integration suites.

Checked-in CodingAgentConfig JSON fixtures plus the ``UCODE_MANAGED_CONFIG_STUB`` env mechanics.
The configure invocation, launch, and assertions stay visible in each test (tests/AGENTS.md), and
this module imports nothing from the ``ucode`` application package (tests/test_integration_contract.py
enforces that boundary).
"""

import os
from pathlib import Path
from urllib.parse import urlparse

MANAGED_CONFIGS_PATH = "/api/ai-gateway/v2/coding-agent-configs"
# One JSON CodingAgentConfig per scenario, in the GET shape `ug configure --file` will accept.
MANAGED_CONFIG_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "managed_config"


def use_managed_config_fixture(session, name: str) -> Path:
    """Point ``UCODE_MANAGED_CONFIG_STUB`` at a checked-in fixture for this session."""
    path = MANAGED_CONFIG_FIXTURES / f"{name}.json"
    assert path.is_file(), f"missing managed-config fixture {path}"
    session.env["UCODE_MANAGED_CONFIG_STUB"] = str(path)
    return path


def assert_no_managed_config(payload: object) -> None:
    """Validate the real List response before claiming unmanaged-workspace coverage."""
    configs = payload.get("coding_agent_configs", []) if isinstance(payload, dict) else payload
    assert isinstance(configs, list) and all(isinstance(config, dict) for config in configs), (
        "Invalid CodingAgentConfig listing; cannot establish an unmanaged workspace"
    )
    names = [config.get("name", "<unnamed>") for config in configs]
    assert not configs, (
        "Unmanaged discovery requires a workspace with no CodingAgentConfig; "
        f"the selected workspace publishes {names}. Use an unmanaged workspace for these "
        "cases. The suite will not remove or bypass shared admin configuration."
    )


def is_managed_config_control_plane_cache(home: Path, path: Path) -> bool:
    """Whether ``path`` is ug's workspace-policy state, not agent-owned configuration.

    That is the installed policy and its refresh stamps, or, where the machine cannot hold the
    installed policy (Windows), the per-user cache of the fetched policy.
    """
    return path in (
        home / ".ucode" / "refresh_ts.json",
        home / ".ucode" / "managed-config.json",
        Path("/etc/unitygateway/managed-settings.json"),
    )


def use_smart_routing_fixture(
    session, agent: str, workspace: str, *, smart_routing: bool = True
) -> Path:
    """Select the checked-in scenario input for the two configured CI workspaces.

    The regions publish different GPT generations. This chooses no workspace and
    retries no request; unsupported workspaces need an explicit fixture assignment.
    ``smart_routing=False`` selects the same models without ``smart_routing.enabled``.
    """
    if agent == "claude":
        name = "claude_smart_routing"
    elif agent == "codex":
        override = os.environ.get("UCODE_TEST_CODEX_ROUTING_FIXTURE", "").strip()
        fixtures = {
            "eng-ml-inference-team-us-east-1.cloud.databricks.com": "codex_smart_routing",
            "eng-ml-inference-team-ca-central-1.cloud.databricks.com": "codex_smart_routing_ca",
        }
        hostname = urlparse(workspace).hostname
        name = override or fixtures.get(hostname)
        assert name, (
            f"No Codex routing fixture assigned to {hostname}; set "
            "UCODE_TEST_CODEX_ROUTING_FIXTURE to a fixture basename validated for that workspace"
        )
    else:
        raise AssertionError(f"No routing fixture for {agent}")
    return use_managed_config_fixture(session, name if smart_routing else f"{name}_off")
