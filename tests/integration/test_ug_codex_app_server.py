"""CUJ: a client connects to Codex's real app-server through ug."""

import pytest

pytestmark = [pytest.mark.live, pytest.mark.codex]


@pytest.mark.parametrize("separator", [False, True], ids=["direct", "launcher-separator"])
@pytest.mark.parametrize(
    "routing",
    ["0", pytest.param("1", marks=pytest.mark.codex_smart_routing)],
    ids=["routing-off", "routing-on"],
)
def test_ug_codex_app_server_client_initializes(live_session, workspace, separator, routing):
    """Scenario: configure Codex and connect a real stdio client to ug codex app-server.

    Expected: initialize returns a valid JSON-RPC result, diagnostics stay off
    the protocol stream, and no routing wrapper starts. The app-server lists ug's
    subagent routing hook with routing on and not with routing off.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    session.env["ENABLE_SMART_ROUTING_V2"] = routing

    args = ["app-server", "--listen", "stdio://"]
    if separator:
        args.insert(0, "--")
    listed = session.app_server_handshake(
        args, request=("hooks/list", {"cwds": [str(session.cwd)]})
    )
    commands = [
        hook.get("command") or "" for entry in listed["result"]["data"] for hook in entry["hooks"]
    ]
    routes_subagents = any("codex-router-hook route-subagent" in command for command in commands)
    assert routes_subagents is (routing == "1"), commands
    session.assert_not_routed()
