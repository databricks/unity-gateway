"""Sending the workspace selection to a host that serves several workspaces.

Every request and every agent config bound to such a host carries the selected workspace; nothing
changes for a single-workspace host.
"""

from __future__ import annotations

import io
import json
from email.message import Message
from unittest.mock import patch

import pytest

import ucode.databricks as db_mod
import ucode.workspace as workspace_mod
from ucode import gateway_proxy, mcp, mcp_connection_login, skills_usage
from ucode.agents import claude, codex, gemini, kilo, opencode, pi
from ucode.state import build_agent_state
from ucode.workspace import (
    activate,
    activate_state,
    pin,
    response_workspace_mismatch,
    signin_redirect_reason,
    workspace_failure_reason,
    workspace_headers,
    workspace_id_args,
)

SHARED = "https://acme.cloud.databricks.com"
CLASSIC = "https://dbc-1.cloud.databricks.com"
WSID = "111"
SELECTOR = {"X-Databricks-Org-Id": WSID, "X-Databricks-Workspace-Id": WSID}


@pytest.fixture
def selected():
    activate(SHARED, WSID)
    yield
    activate(None, None)


class TestWorkspaceHeaders:
    def test_nothing_without_a_selection(self):
        assert workspace_headers(f"{SHARED}/api") == {}

    def test_classic_state_selects_nothing(self):
        activate_state({"workspace": CLASSIC})
        assert workspace_headers(f"{CLASSIC}/api") == {}
        assert workspace_id_args(CLASSIC) == []

    def test_only_for_the_selected_host(self, selected):
        assert workspace_headers(f"{SHARED}/ai-gateway/anthropic/v1/messages") == SELECTOR
        assert workspace_headers(SHARED) == SELECTOR
        assert workspace_headers(f"{CLASSIC}/api") == {}
        assert workspace_headers("https://app-1.aws.databricksapps.com/mcp") == {}
        assert workspace_headers() == SELECTOR

    def test_helper_args(self, selected):
        assert workspace_id_args(SHARED) == ["--workspace-id", WSID]
        assert workspace_id_args(CLASSIC) == []

    def test_stable_connection_id_sends_workspace_id_only(self):
        connection_id = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
        activate(SHARED, connection_id)
        try:
            assert workspace_headers(SHARED) == {"X-Databricks-Workspace-Id": connection_id}
            assert workspace_id_args(SHARED) == ["--workspace-id", connection_id]
            assert response_workspace_mismatch(f"{SHARED}/api", "222") is None
        finally:
            activate(None, None)

    def test_pinned_selection_survives_loading_state(self):
        pin(SHARED, WSID)
        activate_state({"workspace": SHARED, "workspace_id": "222"})
        assert workspace_headers(SHARED)["X-Databricks-Org-Id"] == WSID


class TestActivation:
    def test_configure_selects_the_new_workspace_after_loading_the_previous_one(self, monkeypatch):
        import ucode.cli as cli_mod

        previous = {"workspace": SHARED, "workspace_id": "222"}

        def load_previous():
            activate_state(previous)
            return dict(previous)

        monkeypatch.setattr(cli_mod, "load_state", load_previous)
        monkeypatch.setattr(cli_mod, "save_state", lambda state: None)
        monkeypatch.setattr(cli_mod, "build_shared_base_urls", lambda workspace: {})
        monkeypatch.setattr(cli_mod, "purge_cross_workspace_mcp_residue", lambda *a, **k: None)
        monkeypatch.setattr(cli_mod, "_find_profile_name", lambda workspace, workspace_id: "p")
        cli_mod.configure_shared_state(f"{SHARED}?w={WSID}", skip_preflight=True)
        assert workspace_headers(SHARED) == SELECTOR

    def test_saving_state_without_its_workspace_keeps_the_selection(self, tmp_path):
        from ucode.state import save_state

        save_state({"workspace": SHARED, "workspace_id": WSID})
        save_state({"profile": "p"})
        assert workspace_headers(SHARED) == SELECTOR


class TestFailureReasons:
    def test_no_selection_on_a_multi_workspace_host(self):
        with patch.object(workspace_mod, "host_type", return_value="unified"):
            reason = signin_redirect_reason(f"{SHARED}/api/2.0/x")
        assert "serves several workspaces" in reason
        assert f"{SHARED}?w=<workspace id>" in reason

    def test_unknown_selected_workspace(self, selected):
        assert "workspace 111" in signin_redirect_reason(f"{SHARED}/api/2.0/x")

    def test_single_workspace_host(self):
        with patch.object(workspace_mod, "host_type", return_value="workspace"):
            reason = signin_redirect_reason(f"{CLASSIC}/api/2.0/x")
        assert "token was rejected" in reason
        assert f"databricks auth login --host {CLASSIC}" in reason

    def test_rejected_token_for_the_selected_workspace_keeps_the_sign_in_steps(self, selected):
        reason = workspace_failure_reason(f"{SHARED}/api", 400, "Invalid Token")
        assert reason is not None and "another account" in reason
        assert f"databricks auth login --host '{SHARED}?w={WSID}'" in reason

    def test_account_admin_403_on_a_multi_workspace_host(self):
        with patch.object(workspace_mod, "host_type", return_value="unified"):
            reason = workspace_failure_reason(
                f"{SHARED}/api/2.0/x", 403, "disabled for users without account admin status"
            )
        assert reason is not None and "serves several workspaces" in reason

    def test_other_failures_are_left_alone(self):
        assert workspace_failure_reason(f"{CLASSIC}/api", 403, "PERMISSION_DENIED") is None

    def test_answer_from_another_workspace(self, selected):
        assert response_workspace_mismatch(f"{SHARED}/api", WSID) is None
        assert response_workspace_mismatch(f"{SHARED}/api", None) is None
        assert "222" in (response_workspace_mismatch(f"{SHARED}/api", "222") or "")


class _Response(io.BytesIO):
    def __init__(self, body: bytes, final_url: str, org_id: str | None = None):
        super().__init__(body)
        self._final_url = final_url
        self.status = 200
        self.headers = Message()
        if org_id:
            self.headers["X-Databricks-Org-Id"] = org_id

    def geturl(self):
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class TestHttpChokepoints:
    def _get(self, url, response):
        sent = []

        def urlopen(request, timeout):
            sent.append(request)
            return response

        with patch.object(db_mod.urllib_request, "urlopen", side_effect=urlopen):
            result = db_mod._http_get_json(url, "tok")
        return result, sent[0]

    def test_selected_host_gets_the_headers(self, selected):
        url = f"{SHARED}/api/2.0/x"
        (payload, reason), request = self._get(url, _Response(b"{}", url, WSID))
        assert (payload, reason) == ({}, None)
        assert request.get_header("X-databricks-org-id") == WSID
        assert request.get_header("X-databricks-workspace-id") == WSID

    def test_classic_requests_are_unchanged(self):
        url = f"{CLASSIC}/api/2.0/x"
        _, request = self._get(url, _Response(b"{}", url))
        assert request.get_header("X-databricks-org-id") is None

    def test_sign_in_page_is_reported(self):
        url = f"{SHARED}/api/2.0/x"
        with patch.object(workspace_mod, "host_type", return_value="unified"):
            (payload, reason), _ = self._get(url, _Response(b"<html>", f"{SHARED}/login?next=x"))
        assert payload is None
        assert "serves several workspaces" in (reason or "")

    def test_answer_for_another_workspace_fails(self, selected):
        url = f"{SHARED}/api/2.0/x"
        (payload, reason), _ = self._get(url, _Response(b"{}", url, "222"))
        assert payload is None and "222" in (reason or "")

    def test_probe_reports_the_selection_problem(self):
        reason = workspace_mod.WorkspaceSelectionReason("acme serves several workspaces")
        with (
            patch.object(
                db_mod, "_probe_model_services", return_value=db_mod.GatewayProbe(False, reason)
            ),
            pytest.raises(RuntimeError, match="^acme serves several workspaces$"),
        ):
            db_mod.probe_unity_gateway_capabilities(SHARED, "tok")


class TestHelperArgv:
    def test_mcp_proxy(self, selected):
        argv = db_mod.build_mcp_proxy_argv(f"{SHARED}/api/2.0/mcp/x", SHARED, "p")
        assert argv[argv.index("--workspace-id") + 1] == WSID

    def test_otel_headers(self, selected):
        argv = db_mod.build_otel_headers_argv(SHARED, "p")
        assert argv[argv.index("--workspace-id") + 1] == WSID

    def test_cowork_hook(self, selected):
        from ucode.agents.desktop import cowork

        config = {
            "mcpServers": {
                "gh": {"command": "/bin/ug", "args": ["mcp-proxy", "--url", f"{SHARED}/mcp/gh"]}
            }
        }
        hooks = cowork._connector_hooks(config, SHARED, "p", use_pat=False)
        assert hooks is not None
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert f"--workspace-id {WSID}" in command

    def test_cowork_probe_sends_the_selection(self, selected, monkeypatch):
        import anyio

        from ucode.agents.desktop import cowork_hooks

        seen: dict[str, str] = {}

        class Client:
            def __init__(self, headers, timeout):
                seen.update(headers)
                raise RuntimeError("stop")

        monkeypatch.setattr(cowork_hooks, "_httpx", lambda: type("M", (), {"AsyncClient": Client}))
        with pytest.raises(RuntimeError, match="stop"):
            anyio.run(cowork_hooks._probe, f"{SHARED}/mcp/gh", "tok", 1.0)
        assert seen["X-Databricks-Workspace-Id"] == WSID

    def test_classic_argv_is_unchanged(self):
        assert "--workspace-id" not in db_mod.build_mcp_proxy_argv(f"{CLASSIC}/x", CLASSIC)
        assert "--workspace-id" not in db_mod.build_otel_headers_argv(CLASSIC)


class TestLoopbackProxy:
    def test_proxy_applies_the_selection(self, selected):
        server, cache, client = gateway_proxy.start_otel_proxy(SHARED, lambda force: "tok", port=0)
        try:
            assert server.RequestHandlerClass.workspace_selector == SELECTOR  # type: ignore[attr-defined]
        finally:
            server.server_close()
            cache.stop()
            client.close()

    def test_classic_proxy_adds_nothing(self):
        server, cache, client = gateway_proxy.start_otel_proxy(CLASSIC, lambda force: "tok", port=0)
        try:
            assert server.RequestHandlerClass.workspace_selector == {}  # type: ignore[attr-defined]
        finally:
            server.server_close()
            cache.stop()
            client.close()


class TestMcp:
    def test_native_oauth_entries_are_not_used(self, selected):
        with (
            patch.object(mcp, "workspace_supports_cimd", return_value=True),
            patch.object(mcp, "oauth_client_available", return_value=True),
        ):
            assert mcp._oauth_http_client("claude", SHARED, use_pat=False) is None
            assert (
                mcp._oauth_http_client("claude", CLASSIC, use_pat=False)
                == mcp.PINNED_CIMD_CLIENT_IDS["claude"]
            )

    def test_claude_managed_mcp_stays_in_user_scope(self, selected):
        with (
            patch.object(claude, "managed_files_supported", return_value=True),
            patch.object(claude, "managed_writes_allowed", return_value=True),
            patch.object(claude, "workspace_supports_cimd", return_value=True),
            patch.object(claude, "oauth_client_available", return_value=True),
        ):
            assert not claude.managed_mcp_uses_managed_file(SHARED, use_pat=False)
            assert claude.managed_mcp_uses_managed_file(CLASSIC, use_pat=False)

    def test_connection_sign_in_is_refused(self, selected):
        ok, message = mcp_connection_login.run_connection_login(
            f"{SHARED}/ai-gateway/mcp-services/c.s.svc",
            SHARED,
            profile="p",
        )
        assert not ok and "several workspaces" in message


class TestAgentConfigs:
    def _claude_headers(self, workspace, **kwargs):
        overlay, _ = claude.render_overlay(workspace, "m", **kwargs)
        return overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines()

    def test_claude_selection_comes_after_admin_headers(self, selected):
        lines = self._claude_headers(
            SHARED, managed_http_headers={"x-databricks-org-id": "999", "X-Team": "a"}
        )
        assert "X-Team: a" in lines
        assert lines[-2:] == [f"X-Databricks-Org-Id: {WSID}", f"X-Databricks-Workspace-Id: {WSID}"]
        assert not any(line.startswith("x-databricks-org-id") for line in lines)

    def test_claude_classic_headers_are_unchanged(self):
        assert not any(
            "x-databricks-org-id" in line.lower() for line in self._claude_headers(CLASSIC)
        )

    def test_claude_merge_replaces_the_previous_selection(self, selected):
        existing = "X-Databricks-Org-Id: 222\nX-Mine: keep"
        merged = claude._merge_anthropic_custom_headers(
            existing, f"X-Databricks-Org-Id: {WSID}"
        ).splitlines()
        assert merged == [f"X-Databricks-Org-Id: {WSID}", "X-Mine: keep"]

    def test_claude_merge_drops_a_selection_ug_no_longer_writes(self):
        """Moving to a single-workspace host removes the selection ug wrote for the previous one."""
        merged = claude._merge_anthropic_custom_headers(
            "X-Databricks-Org-Id: 222\nX-Databricks-Workspace-Id: 222\nX-Mine: keep",
            "User-Agent: u",
        )
        assert merged.splitlines() == ["X-Mine: keep", "User-Agent: u"]

    def test_claude_web_search_helper_gets_the_workspace(self, selected):
        entry = claude._web_search_mcp_entry(SHARED, "search-model", "p")
        assert entry["env"]["UG_WORKSPACE_ID"] == WSID
        assert claude._generated_search_entry(entry)
        assert "UG_WORKSPACE_ID" not in claude._web_search_mcp_entry(CLASSIC, "m")["env"]

    def test_codex(self, selected):
        block = codex._provider_block(
            SHARED, "p", managed_http_headers={"X-Databricks-Workspace-Id": "999"}
        )
        assert block["http_headers"]["X-Databricks-Workspace-Id"] == WSID
        assert block["http_headers"]["X-Databricks-Org-Id"] == WSID
        assert "X-Databricks-Org-Id" not in codex._provider_block(CLASSIC, "p")["http_headers"]

    def test_gemini(self, selected):
        env = gemini.render_env_overlay(SHARED, "g", "tok")
        assert f"X-Databricks-Org-Id:{WSID}" in env["GEMINI_CLI_CUSTOM_HEADERS"]
        assert (
            "X-Databricks"
            not in gemini.render_env_overlay(CLASSIC, "g", "tok")["GEMINI_CLI_CUSTOM_HEADERS"]
        )

    def test_pi(self, selected):
        overlay, _ = pi.render_overlay(
            "claude-x",
            "!key",
            {"claude": f"{SHARED}/ai-gateway/anthropic"},
            {"opus": "claude-x"},
            [],
            [],
        )
        headers = next(iter(overlay["providers"].values()))["headers"]
        assert headers["X-Databricks-Org-Id"] == WSID

    @pytest.mark.parametrize("module", [opencode, kilo])
    def test_opencode_and_kilo(self, selected, module):
        base_urls = {"anthropic": f"{SHARED}/ai-gateway/anthropic/v1"}
        overlay, _ = module.render_overlay(
            "claude-x", "tok", base_urls, {"anthropic": ["claude-x"]}
        )
        provider = overlay["provider"]["databricks-anthropic"]
        assert provider["options"]["headers"]["X-Databricks-Org-Id"] == WSID
        assert provider["models"]["claude-x"]["headers"]["X-Databricks-Org-Id"] == WSID

    def test_build_agent_state(self):
        agents = build_agent_state(
            {
                "workspace": SHARED,
                "workspace_id": WSID,
                "base_urls": {"claude": f"{SHARED}/ai-gateway/anthropic"},
            }
        )
        assert agents["claude"]["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines() == [
            f"X-Databricks-Org-Id: {WSID}",
            f"X-Databricks-Workspace-Id: {WSID}",
        ]
        assert agents["codex"]["headers"] == SELECTOR

    def test_build_agent_state_classic(self):
        agents = build_agent_state({"workspace": CLASSIC, "base_urls": {}})
        assert "ANTHROPIC_CUSTOM_HEADERS" not in agents["claude"]["env"]
        assert all("headers" not in config for config in agents.values())


class TestDatabricksApps:
    def test_lists_apps_over_http_with_the_selection(self, selected):
        pages = iter(
            [
                ({"apps": [{"name": "a"}], "next_page_token": "t"}, None),
                ({"apps": [{"name": "b"}]}, None),
            ]
        )
        urls = []
        with (
            patch.object(db_mod, "get_databricks_token", return_value="tok"),
            patch.object(
                db_mod,
                "_http_get_json",
                side_effect=lambda url, token, timeout: urls.append(url) or next(pages),
            ),
            patch.object(db_mod, "run", side_effect=AssertionError("CLI must not run")),
        ):
            apps = db_mod.list_databricks_apps(SHARED, "p")
        assert [app["name"] for app in apps] == ["a", "b"]
        assert "page_token=t" in urls[1]

    def test_forbidden_listing(self, selected):
        with (
            patch.object(db_mod, "get_databricks_token", return_value="tok"),
            patch.object(db_mod, "_http_get_json", return_value=(None, "HTTP 403 Forbidden")),
            pytest.raises(db_mod.PermissionDeniedError),
        ):
            db_mod.list_databricks_apps(SHARED, "p")


class TestSkillsUsageReporter:
    def test_reporter_request_carries_the_selection(self, selected, monkeypatch):
        captured = {}

        class _Reporter:
            stdin = io.StringIO()

        def popen(argv, env, **kwargs):
            captured["request"] = json.loads(env[skills_usage._REPORT_REQUEST_ENV_VAR])
            return _Reporter()

        monkeypatch.setattr(skills_usage.subprocess_cross_os, "popen", popen)
        ref = skills_usage.SkillRef(
            catalog="c", schema="s", securable_name="n", bundle_name="n", skill_id="id-1"
        )
        skills_usage.report_skill_usage_in_background(SHARED, "tok", [ref])
        assert captured["request"]["workspace_id"] == WSID
