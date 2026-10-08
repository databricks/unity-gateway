"""Shared fixture loading and stub injection for managed-config integration suites.

Checked-in CodingAgentConfig JSON fixtures plus the ``UCODE_MANAGED_CONFIG_STUB`` env mechanics.
The configure invocation, launch, and assertions stay visible in each test (tests/AGENTS.md), and
this module imports nothing from the ``ucode`` application package (tests/test_integration_contract.py
enforces that boundary).
"""

from pathlib import Path

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
    """Whether ``path`` is ug's expected fetched-config cache, not agent-owned state."""
    return path == home / ".ucode" / "managed-config.json"
