"""CUJs for the installed ``ug usage`` budget summary."""

import json
import os
import re
from decimal import Decimal

import pytest

_USAGE_SUMMARY = re.compile(
    r"Budget spend:\s+\$(?P<spend>[0-9][0-9,]*\.\d{2})\s+of\s+"
    r"\$(?P<total>[0-9][0-9,]*\.\d{2})\s+\((?P<percent>\d+)%\)"
)
_USAGE_METER = re.compile(r"\[(?P<meter>[█░]{30})\]")
_MANAGED_USAGE_WORKSPACE = "https://eng-ml-inference-team-eu-west-2.cloud.databricks.com"


def _read_managed_cache(session) -> dict:
    path = session.home / ".ucode" / "managed-config.json"
    assert path.is_file(), f"ug did not write the managed-config cache: {path}"
    return json.loads(path.read_text())


@pytest.mark.managed
@pytest.mark.claude
def test_ug_usage_managed_config(live_session):
    """Scenario: configure Claude in the dedicated usage workspace, then run ``ug usage``.

    Expected: the real configure path caches the published CodingAgentConfig for this workspace;
    ``ug usage`` exits successfully and renders parseable dollars, percentage, and a 30-cell
    spend meter without the unavailable fallback.
    """
    session = live_session
    target_bearer = os.environ.get("UG_USAGE_BEARER", "").strip()
    assert target_bearer, (
        "The runner needs UG_USAGE_CLIENT_ID and UG_USAGE_CLIENT_SECRET for the dedicated "
        f"usage workspace ({_MANAGED_USAGE_WORKSPACE})."
    )
    session.env["DATABRICKS_BEARER"] = target_bearer
    session.run("configure", "--workspace", _MANAGED_USAGE_WORKSPACE, "--skip-upgrade", timeout=240)

    cache = _read_managed_cache(session)
    assert cache.get("workspace") == _MANAGED_USAGE_WORKSPACE, cache
    assert cache.get("outcome") == "published", cache
    config = cache.get("config")
    assert isinstance(config, dict), cache
    assert config.get("default_agent"), config
    assert config.get("enabled_agents"), config

    result = session.run("usage", timeout=60)
    assert result.returncode == 0
    output = result.stdout + result.stderr
    assert "Usage information is unavailable." not in output, output
    summary = _USAGE_SUMMARY.search(output)
    assert summary, output
    spend = Decimal(summary.group("spend").replace(",", ""))
    total = Decimal(summary.group("total").replace(",", ""))
    percent = int(summary.group("percent"))
    assert spend >= 0, summary.group(0)
    assert total > 0, summary.group(0)
    assert percent >= 0, summary.group(0)
    meter = _USAGE_METER.search(output)
    assert meter, output
    assert len(meter.group("meter")) == 30, meter.group(0)


@pytest.mark.live
@pytest.mark.claude
def test_ug_usage_without_managed_config(live_session, unmanaged_workspace):
    """Scenario: configure Claude through the public hosted configure command in a normal
    workspace, then run ``ug usage`` where no managed CodingAgentConfig is published.

    Expected: the real configure path records an authoritative ``none`` cache outcome with no
    config, and ``ug usage`` exits zero with the actionable missing-budget guidance and no spend
    summary.
    """
    session = live_session
    workspace = unmanaged_workspace
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    cache = _read_managed_cache(session)
    assert cache.get("workspace") == workspace, cache
    assert cache.get("outcome") == "none", cache
    assert cache.get("config") == {}, cache

    result = session.run("usage", timeout=60)
    assert result.returncode == 0
    output = result.stdout + result.stderr
    normalized_output = " ".join(output.split())
    assert (
        "Usage information is unavailable. Ask your workspace admin to configure "
        "Unity Gateway Budgets and add it to the workspace configuration."
    ) in normalized_output, output
    assert "Budget spend:" not in normalized_output
    assert not re.search(r"\$[0-9][0-9,]*\.\d{2}\s+of\s+\$", normalized_output), output
