"""CUJ: a client connects to Codex's real app-server through ug."""

import pytest

pytestmark = [pytest.mark.live, pytest.mark.codex]


@pytest.mark.parametrize("separator", [False, True], ids=["direct", "launcher-separator"])
@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [
        None,
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
    ids=[
        "unconfigured",
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_ug_codex_app_server_client_initializes(
    live_session, workspace, separator, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: configure Codex and connect a real stdio client under each selector.

    Expected: initialize returns a valid JSON-RPC result, diagnostics stay off
    the protocol stream, and this utility command never starts smart routing.
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
    if SMART_ROUTER_CONFIG_VERSION is not None:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION

    args = ["app-server", "--listen", "stdio://"]
    if separator:
        args.insert(0, "--")
    response = session.app_server_handshake(args)
    assert response["result"]["userAgent"]
    session.assert_not_routed()
