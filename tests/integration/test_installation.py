"""Smoke checks for the installed distribution, outside the source checkout."""

import json

import pytest

pytestmark = pytest.mark.installation


def test_ug_installed_wheel_exposes_help_and_version(session):
    """Scenario: invoke the freshly installed ug console script.

    Expected: public commands are listed and the installed version is reported.
    """
    output = session.run("--help").stdout
    assert "configure" in output and "revert" in output
    assert session.run("--version").stdout.strip()


def test_ug_status_in_fresh_home_is_unconfigured(session):
    """Scenario: inspect ug before any setup in a fresh home.

    Expected: status reports Not Configured and does not create saved state.
    """
    assert "Not Configured" in session.run("status").stdout
    assert not (session.home / ".ucode/state.json").exists()


def test_ug_auth_without_configuration_explains_how_to_configure(session):
    """Scenario: request an auth token before configuring ug.

    Expected: nonzero exit and configure guidance, without a Python traceback.
    """
    result = session.run("auth-token", ok=False)
    assert result.returncode != 0
    assert "configure" in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_ug_and_ucode_auth_helpers_emit_only_the_supplied_bearer(session):
    """Scenario: invoke both installed auth helpers noninteractively with a supplied bearer.

    Expected: normal and forced refresh print only that bearer, with no warnings
    or ANSI escapes. This exercises the supported environment override, not
    workspace authentication, and creates no saved ug state.
    """
    # An explicit input to the public bearer-override API; never sent to a workspace.
    session.env["DATABRICKS_BEARER"] = "ug-integration-supplied-bearer"
    for name in ("ug", "ucode"):
        binary = session.binary.with_name(name)
        assert binary.is_file()
        for refresh in ([], ["--force-refresh"]):
            result = session.run(
                "auth-token",
                "--host",
                "https://workspace.example.invalid",
                *refresh,
                binary=binary,
                strip_ansi=False,
                timeout=30,
            )
            # UserSession redacts the token without changing the rest of stdout.
            assert result.stdout == "<redacted>\n"
            assert result.stderr == ""
    assert not (session.home / ".ucode/state.json").exists()


def test_ug_and_ucode_web_search_helpers_preserve_mcp_stdio(session):
    """Scenario: initialize and list tools through both installed web-search commands.

    Expected: stdout contains exactly the two MCP JSON-RPC responses, with no
    warning text or ANSI escapes. The existing server/tool names are preserved.
    This tests the real local protocol, not a workspace-backed search request.
    """
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    for name in ("ug", "ucode"):
        binary = session.binary.with_name(name)
        assert binary.is_file()
        result = session.run(
            "mcp",
            "web-search",
            binary=binary,
            input_text="".join(json.dumps(request) + "\n" for request in requests),
            strip_ansi=False,
            timeout=30,
        )
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        assert [response["id"] for response in responses] == [1, 2]
        assert all(response["jsonrpc"] == "2.0" for response in responses)
        assert responses[0]["result"]["serverInfo"]["name"] == "ucode-web-search"
        assert [tool["name"] for tool in responses[1]["result"]["tools"]] == ["web_search"]
        assert result.stderr == ""
