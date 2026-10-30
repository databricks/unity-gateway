"""Account APIs and relay plugin generation with external HTTP/auth boundaries isolated."""

import io
import json
import os
import stat
import time
import zipfile
from pathlib import Path

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from ucode import anthropic_mcp_proxy, desktop_auth, state
from ucode.agents.desktop import anthropic_cowork as account
from ucode.agents.desktop import cowork
from ucode.cli import app

WORKSPACE = "https://workspace.example"
MARKDOWN = b"---\nname: docs\ndescription: Edit docs\n---\nUse Claude Docs.\n"
SKILL_FILES = {"SKILL.md": account.SkillFile(MARKDOWN)}


def skill_zip(name="docs/SKILL.md", content=MARKDOWN, mode=0):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        archive.writestr(info, content)
    return output.getvalue()


def api_response(
    request,
    *,
    enabled=True,
    skills=None,
    connectors=None,
    failed=(),
    archives=None,
    catalog_status=200,
):
    assert request.url.host == "api.anthropic.com"
    assert request.headers["Authorization"] == "Bearer fixture-token"
    if request.url.path == "/api/oauth/profile":
        return httpx.Response(200, json={"organization": {"uuid": "org-id"}})
    if request.url.path == "/v1/mcp_servers":
        assert request.headers["anthropic-beta"] == "mcp-servers-2025-12-04"
        return httpx.Response(
            200,
            json={
                "data": connectors
                if connectors is not None
                else [
                    {
                        "display_name": "Claude Docs",
                        "id": "server-id",
                        "eligible": True,
                    }
                ]
            },
        )
    assert request.headers["x-organization-uuid"] == "org-id"
    if request.url.path.endswith("/list-skills"):
        return httpx.Response(
            catalog_status,
            json={"skills": skills if skills is not None else [{"id": "docs", "enabled": enabled}]},
        )
    assert request.url.path.endswith("/download")
    skill_id = request.url.path.split("/")[-2]
    if skill_id in failed:
        return httpx.Response(503)
    content = (archives or {}).get(skill_id, skill_zip(f"{skill_id}/SKILL.md"))
    return httpx.Response(200, content=content)


@pytest.fixture
def account_http(monkeypatch):
    # Only the remote API and credential store are replaced; the client and writers remain real.
    original = httpx.Client
    monkeypatch.setattr(account, "get_claude_subscription_token", lambda _: "fixture-token")
    handler = {"enabled": True}
    monkeypatch.setattr(
        account.httpx,
        "Client",
        lambda **kwargs: original(
            **{
                **kwargs,
                "transport": httpx.MockTransport(lambda request: api_response(request, **handler)),
            }
        ),
    )
    return handler


def test_account_loads_enabled_docs_and_connector_from_same_credential(account_http):
    resources = account.load_resources("/opt/claude")
    assert resources.connectors == {"claude-docs": "server-id"}
    assert resources.skills == [account.AccountSkill("docs", "docs", SKILL_FILES)]
    assert resources.claude_binary == "/opt/claude"


def test_disabled_docs_is_not_downloaded(account_http):
    account_http["enabled"] = False
    assert account.load_resources("/opt/claude").skills == []


def test_imports_all_eligible_connectors_and_enabled_skills(account_http, tmp_path):
    account_http["connectors"] = [
        {"id": "server-id", "display_name": "Claude Docs", "eligible": True},
        {"id": "drive-id", "display_name": "Google Drive", "eligible": True},
        {"id": "blocked", "display_name": "Blocked", "eligible": False},
        {"id": "unknown", "display_name": "Unknown"},
    ]
    account_http["skills"] = [
        {"id": "docs", "name": "docs", "enabled": True},
        {"id": "google-workspace", "name": "google-workspace", "enabled": True},
        {"id": "custom-id", "name": "my-team", "enabled": True},
        {"id": "disabled", "enabled": False},
        {"id": "unknown"},
    ]
    account_http["archives"] = {"custom-id": skill_zip("custom-name/SKILL.md")}
    account_http["failed"] = ("disabled", "unknown")
    resources = account.load_resources("/opt/claude")
    assert resources.connectors == {"claude-docs": "server-id", "google-drive": "drive-id"}
    assert {skill.id for skill in resources.skills} == {"docs", "google-workspace", "custom-id"}
    root = tmp_path / "plugin"
    account.write_plugin(root, resources, "/opt/python", WORKSPACE)
    config = json.loads((root / ".mcp.json").read_text())["mcpServers"]
    assert set(config) == {"claude-docs", "google-drive"}
    assert config["google-drive"]["args"][6] == "drive-id"
    assert {p.name for p in (root / "skills").iterdir()} == {"docs", "google-workspace", "my-team"}
    assert (root / "skills/my-team/SKILL.md").read_bytes() == MARKDOWN


def test_skills_import_does_not_require_claude_docs_connector(account_http):
    account_http["connectors"] = []
    resources = account.load_resources("/opt/claude")
    assert resources.connectors == {}
    assert resources.skills[0].name == "docs"


def test_resource_aliases_handle_duplicate_and_unsafe_names_without_order_dependence():
    catalog = [
        {"id": "a", "name": "../same name"},
        {"id": "b", "name": "Same Name"},
        {"id": "c", "name": "../../"},
        {"id": "d", "name": "x" * 200},
    ]
    aliases = account._aliases(catalog, "name")
    assert aliases == account._aliases(list(reversed(catalog)), "name")
    assert len(set(aliases.values())) == 4
    assert aliases["a"].startswith("same-name-")
    assert aliases["c"] == "resource"
    assert all(len(name) <= 64 and "/" not in name for name in aliases.values())


def test_hook_refreshes_all_skills_retains_failed_and_removes_disabled(account_http, tmp_path):
    account_http["skills"] = [
        {"id": name, "name": name, "enabled": True}
        for name in ("docs", "google-workspace", "custom", "deleted")
    ]
    root = tmp_path / "plugin"
    account.write_plugin(root, account.load_resources("/opt/claude"), "/opt/python", WORKSPACE)
    custom = root / "skills/custom/SKILL.md"
    custom.write_text("last good skill")
    account_http["skills"] = [
        {"id": "docs", "enabled": False},
        {"id": "google-workspace", "enabled": True},
        {"id": "custom", "enabled": True},
        {"id": "new", "enabled": True},
    ]
    account_http["failed"] = ("custom",)
    account.sync_hook({"hook_event_name": "SessionStart"}, root, "/opt/claude")
    assert custom.read_text() == "last good skill"
    assert not (root / "skills/docs").exists()
    assert not (root / "skills/deleted").exists()
    assert (root / "skills/new/SKILL.md").read_bytes() == MARKDOWN


def test_skill_failure_does_not_block_connectors_or_other_skills(account_http, tmp_path):
    account_http["skills"] = [{"id": name, "enabled": True} for name in ("docs", "bad")]
    account_http["failed"] = ("bad",)
    root = tmp_path / "plugin"
    resources = account.load_resources("/opt/claude")
    account.write_plugin(root, resources, "/opt/python", WORKSPACE)
    assert (root / "skills/docs/SKILL.md").exists()
    assert not (root / "skills/bad").exists()
    assert "claude-docs" in json.loads((root / ".mcp.json").read_text())["mcpServers"]


def test_failed_skill_is_not_retained_across_organizations(tmp_path):
    root = tmp_path / "plugin"
    resources = account.AccountResources(
        {}, [account.AccountSkill("id", "custom", SKILL_FILES)], "old", "/opt/claude", ""
    )
    account.write_plugin(root, resources, "/opt/python", WORKSPACE)
    resources.org = "new"
    resources.skills[0].files = None
    account.write_plugin(root, resources, "/opt/python", WORKSPACE)
    assert not (root / "skills/custom").exists()


def test_failed_skill_retains_previous_copy_when_alias_changes(tmp_path):
    root = tmp_path / "plugin"
    resources = account.AccountResources(
        {}, [account.AccountSkill("id", "old-name", SKILL_FILES)], "org", "/opt/claude", ""
    )
    account.write_plugin(root, resources, "/opt/python", WORKSPACE)
    resources.skills = [account.AccountSkill("id", "new-name", None)]
    account.write_plugin(root, resources, "/opt/python", WORKSPACE)
    assert (root / "skills/new-name/SKILL.md").read_bytes() == MARKDOWN
    assert not (root / "skills/old-name").exists()


def test_discovery_failure_preserves_existing_account_plugin(tmp_path, account_http):
    marketplace = tmp_path / "marketplace"
    workspace_plugin = marketplace / "plugins" / cowork.PLUGIN_NAME
    config = {"mcpServers": {}}
    cowork._write_marketplace(
        marketplace,
        workspace_plugin,
        [],
        config,
        anthropic_resources=account.load_resources("/opt/claude"),
        workspace=WORKSPACE,
    )
    account_root = marketplace / "plugins" / account.PLUGIN_NAME
    original = (account_root / ".mcp.json").read_bytes()
    cowork._write_marketplace(marketplace, workspace_plugin, [], config, preserve_anthropic=True)
    assert (account_root / ".mcp.json").read_bytes() == original
    assert (account_root / "skills/docs/SKILL.md").read_bytes() == MARKDOWN
    manifest = json.loads((marketplace / ".claude-plugin/marketplace.json").read_text())
    assert account.PLUGIN_NAME in {plugin["name"] for plugin in manifest["plugins"]}


def test_hook_catalog_failure_preserves_all_installed_skills(tmp_path, account_http):
    root = tmp_path / "plugin"
    account.write_plugin(root, account.load_resources("/opt/claude"), "/opt/python", WORKSPACE)
    account_http["catalog_status"] = 503
    with pytest.raises(RuntimeError, match="HTTP 503"):
        account.sync_hook({"hook_event_name": "SessionStart"}, root, "/opt/claude")
    assert (root / "skills/docs/SKILL.md").read_bytes() == MARKDOWN


def test_upgrades_docs_only_layout_and_removes_disabled_docs(tmp_path, account_http):
    root = tmp_path / "plugin"
    account.write_plugin(root, account.load_resources("/opt/claude"), "/opt/python", WORKSPACE)
    (root / account.SKILL_MANIFEST).unlink()
    sibling = root / "skills/unowned"
    sibling.mkdir()
    (sibling / "SKILL.md").write_text("keep")
    account_http["enabled"] = False
    account.sync_hook({"hook_event_name": "SessionStart"}, root, "/opt/claude")
    assert not (root / "skills/docs").exists()
    assert (sibling / "SKILL.md").read_text() == "keep"


def test_hook_rolls_back_skills_if_manifest_replacement_fails(tmp_path, account_http, monkeypatch):
    root = tmp_path / "plugin"
    account.write_plugin(root, account.load_resources("/opt/claude"), "/opt/python", WORKSPACE)
    (root / "skills/docs/SKILL.md").write_text("last good skill")
    previous_manifest = (root / account.SKILL_MANIFEST).read_bytes()
    replace = os.replace

    def fail_manifest(source, destination):
        if Path(destination) == root / account.SKILL_MANIFEST:
            raise OSError("disk unavailable")
        return replace(source, destination)

    monkeypatch.setattr(account.os, "replace", fail_manifest)
    with pytest.raises(OSError, match="disk unavailable"):
        account.sync_hook({"hook_event_name": "SessionStart"}, root, "/opt/claude")
    assert (root / "skills/docs/SKILL.md").read_text() == "last good skill"
    assert (root / account.SKILL_MANIFEST).read_bytes() == previous_manifest


@pytest.mark.parametrize(
    "result", [{"skills": [{}], "has_more": True}, {"skills": "bad"}, {"skills": [None]}]
)
def test_incomplete_or_malformed_catalog_is_not_treated_as_deletions(result):
    with pytest.raises(RuntimeError):
        account._catalog(result, "skills")


def test_api_reads_fresh_token_per_request_and_does_not_follow_redirects():
    tokens = iter(["first", "second"])
    seen = []

    def handle(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(302, headers={"location": "https://external.example/"})

    with account.AccountClient(
        "/opt/claude",
        token_provider=lambda: next(tokens),
        transport=httpx.MockTransport(handle),
    ) as client:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="HTTP 302"):
                client.get("/api/oauth/profile")
        with pytest.raises(RuntimeError, match="relative API path"):
            client.get("https://external.example/")
    assert seen == ["Bearer first", "Bearer second"]


def test_api_error_does_not_include_response_body_or_credential():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, json={"error": "fixture-secret"})
    )
    with account.AccountClient(
        "/opt/claude",
        token_provider=lambda: "fixture-token",
        transport=transport,
    ) as client:
        with pytest.raises(RuntimeError) as error:
            client.connectors()
    assert str(error.value) == "Anthropic account API returned HTTP 403."


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/outside",
        "docs/../../outside",
        "docs/..\\outside",
        "C:/outside",
        "docs/.hidden/SKILL.md",
    ],
)
def test_archive_rejects_paths_outside_owned_skill_files(name):
    with pytest.raises(RuntimeError, match="unsafe path"):
        account.unpack_skill(skill_zip(name))


def test_archive_rejects_symlinks_and_invalid_markdown():
    with pytest.raises(RuntimeError, match="unsafe path"):
        account.unpack_skill(skill_zip(mode=stat.S_IFLNK | 0o777))
    with pytest.raises(RuntimeError, match="valid SKILL.md"):
        account.unpack_skill(skill_zip(content=b"not a skill"))


def test_archive_preserves_support_files_and_executable_permissions(tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("team-skill/SKILL.md", MARKDOWN)
        script = zipfile.ZipInfo("team-skill/scripts/run.sh")
        script.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(script, b"#!/bin/sh\ntrue\n")
        archive.writestr("team-skill/references/guide.md", b"reference")
    account.write_skill(tmp_path, account.unpack_skill(output.getvalue()))
    assert (tmp_path / "references/guide.md").read_bytes() == b"reference"
    assert (tmp_path / "scripts/run.sh").stat().st_mode & 0o111


@pytest.mark.parametrize(
    "names", [("SKILL.md", "skill.md"), ("scripts", "scripts/run.sh"), ("docs",)]
)
def test_archive_rejects_conflicting_paths_and_missing_skill_root(names):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in names:
            archive.writestr(name, MARKDOWN)
    with pytest.raises(RuntimeError):
        account.unpack_skill(output.getvalue())


def test_relay_plugin_coexists_with_workspace_docs_and_mcp(tmp_path):
    marketplace = tmp_path / "marketplace"
    workspace_skill = tmp_path / ".claude" / "skills" / "docs"
    workspace_skill.mkdir(parents=True)
    (workspace_skill / "SKILL.md").write_text("workspace docs")
    config = {"mcpServers": {"system-ai": {"type": "stdio", "command": "/opt/ug"}}}
    resources = account.AccountResources(
        {"claude-docs": "server-id"},
        [account.AccountSkill("docs", "docs", SKILL_FILES)],
        "org-id",
        "/opt/claude",
        "",
    )
    cowork._write_marketplace(
        marketplace,
        marketplace / "plugins" / cowork.PLUGIN_NAME,
        [{"bundle_name": "docs", "dirs": [str(workspace_skill)]}],
        config,
        anthropic_resources=resources,
        workspace=WORKSPACE,
    )
    regular = marketplace / "plugins" / cowork.PLUGIN_NAME
    relay = marketplace / "plugins" / account.PLUGIN_NAME
    assert (regular / "skills/docs/SKILL.md").read_text() == "workspace docs"
    assert json.loads((regular / ".mcp.json").read_text()) == config
    assert (relay / "skills/docs/SKILL.md").read_bytes() == MARKDOWN
    entry = json.loads((relay / ".mcp.json").read_text())["mcpServers"]["claude-docs"]
    assert Path(entry["command"]).is_absolute()
    assert entry["args"][:3] == ["-m", "ucode.cli", "anthropic-mcp-proxy"]
    assert entry["args"][-1] == ""
    hooks = json.loads((relay / "hooks/hooks.json").read_text())
    assert "--anthropic" in hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    manifest = json.loads((marketplace / ".claude-plugin/marketplace.json").read_text())
    assert {p["name"] for p in manifest["plugins"]} == {cowork.PLUGIN_NAME, account.PLUGIN_NAME}
    assert "fixture-token" not in (relay / ".mcp.json").read_text()


def test_start_hook_refreshes_and_removes_only_owned_docs(tmp_path, account_http):
    resources = account.load_resources("/opt/claude")
    root = tmp_path / "plugin"
    account.write_plugin(root, resources, "/opt/python", WORKSPACE)
    sibling = root / "skills" / "other"
    sibling.mkdir()
    (sibling / "SKILL.md").write_text("keep")
    docs = root / "skills/docs/SKILL.md"
    docs.write_text("stale")
    payload = {"hook_event_name": "SessionStart"}
    output = account.sync_hook(payload, root, "/opt/claude")
    assert docs.read_bytes() == MARKDOWN
    assert output["hookSpecificOutput"]["reloadSkills"] is True
    account_http["enabled"] = False
    account.sync_hook(payload, root, "/opt/claude")
    assert not docs.exists()
    assert (sibling / "SKILL.md").read_text() == "keep"
    assert account.sync_hook({"hook_event_name": "UserPromptSubmit"}, root, "/opt/claude") == {}


def test_credential_source_does_not_use_inherited_cowork_keychain(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cowork/account/org")
    monkeypatch.setenv("CLAUDE_HELPER_CONTEXT", "interactive")
    with account.credential_source(""):
        assert desktop_auth._claude_keychain_service() == "Claude Code-credentials"
        assert os.environ["CLAUDE_HELPER_CONTEXT"] == "background"
    assert os.environ["CLAUDE_CONFIG_DIR"] == "/cowork/account/org"
    assert os.environ["CLAUDE_HELPER_CONTEXT"] == "interactive"


def test_subscription_token_does_not_require_databricks_auth(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        desktop_auth,
        "_read_claude_credentials",
        lambda: {
            "claudeAiOauth": {
                "accessToken": "fixture-token",
                "expiresAt": (time.time() + 7200) * 1000,
            }
        },
    )
    assert desktop_auth.get_claude_subscription_token("/opt/claude") == "fixture-token"


def test_proxy_reloads_subscription_credential_for_each_request(monkeypatch):
    tokens = iter(["first", "second"])
    monkeypatch.setattr(
        anthropic_mcp_proxy, "get_claude_subscription_token", lambda _: next(tokens)
    )
    auth = anthropic_mcp_proxy.token_auth("/opt/claude")

    async def exercise():
        seen = []
        for _ in range(2):
            request = httpx.Request("POST", "https://mcp-proxy.anthropic.com/v1/mcp/id")
            flow = auth.async_auth_flow(request)
            sent = await anext(flow)
            seen.append(sent.headers["Authorization"])
            await flow.aclose()
        assert seen == ["Bearer first", "Bearer second"]

    anyio.run(exercise)


def test_non_relay_workspace_cannot_start_anthropic_proxy():
    state.save_state({"workspace": WORKSPACE, "claude_relayed": False})
    result = CliRunner().invoke(
        app,
        [
            "anthropic-mcp-proxy",
            "--host",
            WORKSPACE,
            "--server-id",
            "server-id",
            "--claude-binary",
            "/opt/claude",
        ],
    )
    assert result.exit_code == 1
    assert "configured relay workspace" in result.output


def test_proxy_allows_other_eligible_connectors(account_http, monkeypatch):
    account_http["connectors"] = [
        {"id": "drive-id", "display_name": "Google Drive", "eligible": True}
    ]
    calls = []
    # The stdio process boundary is not started in this eligibility check.
    monkeypatch.setattr(
        anthropic_mcp_proxy.anyio, "run", lambda function, *args: calls.append(args)
    )
    anthropic_mcp_proxy.serve("drive-id", "/opt/claude")
    assert calls == [("drive-id", "/opt/claude")]


def test_proxy_rejects_removed_or_ineligible_connector(account_http):
    account_http["connectors"] = [
        {"id": "server-id", "display_name": "Claude Docs", "eligible": False}
    ]
    with pytest.raises(SystemExit) as error:
        anthropic_mcp_proxy.serve("server-id", "/opt/claude")
    assert error.value.code == 2


def test_stdio_bridge_preserves_instructions_schemas_and_tool_results(monkeypatch):
    from contextlib import asynccontextmanager

    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage
    from pydantic import TypeAdapter

    adapter = TypeAdapter(JSONRPCMessage)
    http = anthropic_mcp_proxy._httpx()
    original_client = http.AsyncClient
    requests = []
    monkeypatch.setattr(
        anthropic_mcp_proxy, "get_claude_subscription_token", lambda _: "fixture-token"
    )

    def respond(request):
        assert request.url.host == "mcp-proxy.anthropic.com"
        assert request.url.path == "/v1/mcp/server-id"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if request.method == "GET":
            return http.Response(405)
        if request.method == "DELETE":
            return http.Response(200)
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["method"] == "notifications/initialized":
            return http.Response(202)
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "docs", "version": "1"},
                "instructions": "Connector-specific instructions",
            }
        elif payload["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "guide",
                        "description": "Read docs instructions",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "items": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        else:
            assert payload["method"] == "tools/call"
            assert payload["params"] == {
                "name": "guide",
                "arguments": {"items": ["topic.instructions"]},
            }
            result = {"content": [{"type": "text", "text": "Guide text"}], "isError": False}
        return http.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": result,
            },
            headers={"Mcp-Session-Id": "fixture-session"},
        )

    # In-memory transport boundaries exercise the real MCP SDK and both proxy pumps.
    monkeypatch.setattr(
        http,
        "AsyncClient",
        lambda **kwargs: original_client(**kwargs, transport=http.MockTransport(respond)),
    )

    async def exercise():
        client_send, proxy_read = anyio.create_memory_object_stream(10)
        proxy_write, client_read = anyio.create_memory_object_stream(10)

        @asynccontextmanager
        async def stdio():
            yield proxy_read, proxy_write

        monkeypatch.setattr(anthropic_mcp_proxy, "stdio_server", stdio)

        async def exchange(request_id, method, params):
            await client_send.send(
                SessionMessage(
                    adapter.validate_python(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": method,
                            "params": params,
                        }
                    )
                )
            )
            message = (await client_read.receive()).message
            return getattr(message, "root", message).result

        with anyio.fail_after(10):
            async with anyio.create_task_group() as group:
                group.start_soon(anthropic_mcp_proxy._run, "server-id", "/opt/claude")
                result = await exchange(
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "unit-test", "version": "1"},
                    },
                )
                assert result["instructions"] == "Connector-specific instructions"
                await client_send.send(
                    SessionMessage(
                        adapter.validate_python(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/initialized",
                            }
                        )
                    )
                )
                tools = (await exchange(2, "tools/list", {}))["tools"]
                assert tools[0]["inputSchema"]["properties"]["items"]["type"] == "array"
                assert tools[0]["annotations"]["readOnlyHint"] is True
                result = await exchange(
                    3,
                    "tools/call",
                    {
                        "name": "guide",
                        "arguments": {"items": ["topic.instructions"]},
                    },
                )
                assert result == {
                    "content": [{"type": "text", "text": "Guide text"}],
                    "isError": False,
                }
                await client_send.aclose()
        assert [r["method"] for r in requests] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/call",
        ]

    anyio.run(exercise)


def test_configure_installs_relay_plugin_and_removes_it_after_switching_modes(
    tmp_path,
    monkeypatch,
    account_http,
):
    desktop = tmp_path / "Claude-3p"
    root = desktop / "local-agent-mode-sessions/account/org"
    root.mkdir(parents=True)
    monkeypatch.setattr(cowork, "claude_desktop_root_directory", lambda: desktop)
    monkeypatch.setattr(cowork, "records_for_scope", lambda *_: [])
    monkeypatch.setattr(cowork.shutil, "which", lambda name: f"/opt/{name}")
    calls = []
    from types import SimpleNamespace

    installed = []

    def command(argv, **kwargs):
        calls.append(argv)
        payload = installed if argv[1:4] == ["plugin", "list", "--cowork"] else {"outcome": "ok"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", command)
    configured = {"workspace": WORKSPACE, "profile": "chosen", "claude_relayed": True}
    assert cowork.configure_cowork(configured, [])
    marketplace = cowork._marketplace_root()
    assert (marketplace / "plugins" / account.PLUGIN_NAME / "skills/docs/SKILL.md").exists()
    assert ["plugin", "install", account.PLUGIN_ID] in [c[1:4] for c in calls]
    installed[:] = [{"pluginId": cowork.PLUGIN_ID}, {"pluginId": account.PLUGIN_ID}]
    configured["claude_relayed"] = False
    assert cowork.configure_cowork(configured, [])
    assert not (marketplace / "plugins" / account.PLUGIN_NAME).exists()
    assert ["plugin", "uninstall", account.PLUGIN_ID] in [c[1:4] for c in calls]


def test_hidden_hook_emits_reload_json_for_relay_only(tmp_path, monkeypatch, account_http):
    root = tmp_path / "plugin"
    resources = account.load_resources("/opt/claude")
    account.write_plugin(root, resources, "/opt/python", WORKSPACE)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cowork/account/org")
    state.save_state({"workspace": WORKSPACE, "claude_relayed": True})
    args = [
        "cowork-hook",
        "--host",
        WORKSPACE,
        "--anthropic",
        "--claude-binary",
        "/opt/claude",
        "--claude-config-dir",
        "",
    ]
    result = CliRunner().invoke(app, args, input='{"hook_event_name":"SessionStart"}')
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["reloadSkills"] is True
    assert "fixture-token" not in result.output
    state.save_state({"workspace": WORKSPACE, "claude_relayed": False})
    result = CliRunner().invoke(app, args, input='{"hook_event_name":"SessionStart"}')
    assert result.exit_code == 0
    assert result.stdout == ""
