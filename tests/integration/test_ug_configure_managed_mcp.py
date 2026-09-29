"""Managed-config CUJ: admin MCP servers reach the agent, additive to the developer's own servers.

The admin CodingAgentConfig is injected via UCODE_MANAGED_CONFIG_STUB so the real configure path
can be driven against an MCP list the live workspace does not publish; only the config INPUT is
stubbed (auth, the config writers, the OS-managed reconcile, and the agent binary stay real). See
tests/AGENTS.md rule 4.

An interactive `ug configure` (a PTY, so ug performs its sudo OS-managed reconcile) writes the
managed servers into Codex's `[mcp_servers]` in managed_config.toml, additive to and separate from
the developer's own ~/.codex/config.toml servers. A non-interactive configure has no sudo reconcile,
so it falls back to the user-scope registration, which the real /mcp view still lists. Claude's
`managedMcpServers` path is covered by unit tests; its integration coverage needs the managed
workspace to publish the `claude-code` OAuth client, which E2E_ADMIN_WORKSPACE does not.
"""

import tomllib

import pytest
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
    set_managed_config_stub,
)
from utils.terminal import AgentTerminal, ConfigureTerminal

CLAUDE_OPUS = "system.ai.claude-opus-4-8"
CODEX_MODEL = "system.ai.gpt-5-6-sol"
# A real ca-central MCP service; the live published config lists no MCP servers, so its appearance
# can only come from the injected config.
MCP_SERVICE = "system.ai.github"
CODEX_MANAGED_CONFIG_PATH = "/etc/codex/managed_config.toml"


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_mcp_lists_configured_server(live_session, workspace, tmp_path):
    """Scenario: a non-interactive configure with a managed MCP server, then open Claude's /mcp.

    Expected: with no sudo reconcile available, ug registers the server at user scope and the real
    /mcp view lists it.
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE",
        build_claude_agent_config([CLAUDE_OPUS]),
        mcp_names=[MCP_SERVICE],
    )
    set_managed_config_stub(session, tmp_path, config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "managed-mcp") as tui:
        tui.boot()
        tui.send("/mcp", "type the /mcp command")
        tui.send("\r", "open the MCP list")
        tui.wait_for(
            lambda s: "github" in s.lower(),
            "the /mcp view to list the managed MCP server",
            timeout=60,
        )


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_managed_fixture_codex_mcp_written_to_managed_file(live_session, workspace, tmp_path):
    """Scenario: an interactive configure with a managed MCP server writes Codex's managed file.

    Expected: the server lands in the `[mcp_servers]` table of the OS-managed config as the ug
    mcp-proxy stdio command, and the developer's own ~/.codex/config.toml is not touched.
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CODEX",
        build_codex_agent_config(models=[CODEX_MODEL]),
        mcp_names=[MCP_SERVICE],
    )
    set_managed_config_stub(session, tmp_path, config)
    command = [str(session.binary), "configure", "--workspace", workspace, "--skip-upgrade"]
    with ConfigureTerminal(session, "codex", command, "managed-mcp-file-codex") as configure:
        configure.finish(timeout=300)

    managed = tomllib.loads(session.run(CODEX_MANAGED_CONFIG_PATH, binary="cat", timeout=30).stdout)
    servers = managed.get("mcp_servers") or {}
    entries = {name: entry for name, entry in servers.items() if "github" in name.lower()}
    assert entries, managed
    entry = next(iter(entries.values()))
    assert entry["command"].endswith("ug"), entry
    assert "--url" in entry["args"], entry
    assert "mcp-services" in entry["args"][entry["args"].index("--url") + 1], entry

    personal_path = session.home / ".codex" / "config.toml"
    personal = tomllib.loads(personal_path.read_text()) if personal_path.exists() else {}
    personal_servers = personal.get("mcp_servers") or {}
    assert not any("github" in name.lower() for name in personal_servers), personal_servers


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_managed_fixture_codex_http_headers_in_managed_file(live_session, workspace, tmp_path):
    """Scenario: an interactive configure with managed http_headers writes them into the OS-managed file.

    Scope: managed-config content assertion in the same family as the MCP/skills/models
    managed_fixture tests — verifies the http_headers field flows from the injected
    CodingAgentConfig through `ug configure codex` into the OS-managed
    /etc/codex/managed_config.toml under [model_providers.Databricks.http_headers].

    Expected: after an interactive PTY configure, /etc/codex/managed_config.toml contains
    model_providers.Databricks.http_headers with the admin-specified header
    x-databricks-workspace = "eng-ml-inference", exactly as the injected managed config
    dictates.
    """
    session = live_session
    managed_header_key = "x-databricks-workspace"
    managed_header_value = "eng-ml-inference"
    config = build_coding_agent_config(
        "CODING_AGENT_CODEX",
        build_codex_agent_config(
            models=[CODEX_MODEL],
            http_headers={managed_header_key: managed_header_value},
        ),
    )
    set_managed_config_stub(session, tmp_path, config)
    command = [str(session.binary), "configure", "--workspace", workspace, "--skip-upgrade"]
    with ConfigureTerminal(session, "codex", command, "managed-http-headers-codex") as configure:
        configure.finish(timeout=300)

    managed = tomllib.loads(session.run(CODEX_MANAGED_CONFIG_PATH, binary="cat", timeout=30).stdout)
    provider = (managed.get("model_providers") or {}).get("Databricks") or {}
    headers = provider.get("http_headers") or {}
    assert managed_header_key in headers, (
        f"Expected header {managed_header_key!r} in model_providers.Databricks.http_headers; "
        f"got: {headers!r}\nFull managed config: {managed!r}"
    )
    assert headers[managed_header_key] == managed_header_value, (
        f"Expected {managed_header_key!r} = {managed_header_value!r}, "
        f"got {headers[managed_header_key]!r}"
    )
