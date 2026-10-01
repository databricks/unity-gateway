"""Adapter that drives the pre-interface agent modules through :class:`Agent`.

Every ``agents/<name>.py`` module predates :mod:`ucode.agents.interface`: ug core reached into
each one directly and kept the per-agent ``if tool == ...`` branches for itself. Those branches
live here instead, keyed by the agent each :class:`LegacyAgent` wraps, so core can talk to all
of them through one interface. :class:`LegacyMcpClient` does the same for MCP registration, where
the client set is slightly different (Cursor takes MCP servers but is not an agent).

This file only shrinks. As an agent grows a native class implementing :class:`Agent`, delete
its branches here and drop it from :data:`LEGACY_MODULES`; when the table is empty, so is this
module. Hooks are looked up on the wrapped module at call time rather than bound once, so an
agent can keep moving its own internals without this adapter noticing.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import ModuleType

from ucode.config_io import restore_file
from ucode.constants import MCP_CLEANUP_SCOPES, MCP_USER_SCOPE
from ucode.mcp_oauth import (
    CLAUDE_CODE_OAUTH_CLIENT_ID,
    CODEX_CLI_OAUTH_CLIENT_ID,
    CURSOR_OAUTH_CLIENT_ID,
)

from . import claude, codex, cursor, gemini
from .args import LaunchOptions
from .interface import ConfigureRequest, Install, McpServer, Models

# The agent modules this adapter wraps, in the order ug lists agents.
LEGACY_MODULES: dict[str, ModuleType] = {
    "codex": codex,
    "claude": claude,
    "gemini": gemini,
}

# Agents with their own self-updater, which ug prefers over npm when the binary is installed.
_NATIVE_UPGRADE_ARGV: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "upgrade"),
    "codex": ("codex", "update"),
}

# Agents that write the first resolved model into their ug config, so ug knows the starting
# model. Claude and Codex deliberately leave that choice to the agent unless a model is pinned.
_PINS_FIRST_MODEL = frozenset({"gemini"})


def model_values(value: object) -> list[str]:
    """Flatten a state model inventory (str, list, or provider-keyed dict) into model ids."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    if isinstance(value, dict):
        return [model for models in value.values() for model in model_values(models)]
    return []


class LegacyAgent:
    """One pre-interface agent module, presented as an :class:`Agent`."""

    def __init__(self, tool: str, module: ModuleType) -> None:
        self._tool = tool
        self._module = module
        self.display = str(module.SPEC["display"])
        # Pi has no MCP support today, so it is the one agent with no client (see LEGACY_MCP_CLIENTS).
        self.mcp = LEGACY_MCP_CLIENTS.get(tool)
        self.install = Install(
            binary=str(module.SPEC["binary"]),
            package=str(module.SPEC["package"]),
            upgrade_argv=_NATIVE_UPGRADE_ARGV.get(tool),
            version_error=(
                self._version_error if hasattr(module, "minimum_version_error") else None
            ),
            too_new=self._too_new if hasattr(module, "too_new_downgrade") else None,
            before_install=self._detach_app_model_catalog if tool == "codex" else None,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LegacyAgent({self._tool!r})"

    # -- install ------------------------------------------------------------------------

    def _version_error(self) -> str | None:
        return self._module.minimum_version_error()

    def _too_new(self) -> tuple[str, str] | None:
        return self._module.too_new_downgrade()

    def _detach_app_model_catalog(self) -> None:
        # Codex only: drop ug's shared app catalog reference before the binary changes, so a
        # version that can no longer read it never starts against it.
        self._module.detach_app_model_catalog()

    # -- models -------------------------------------------------------------------------

    def models(self, state: dict) -> Models:
        available = tuple(dict.fromkeys(self._available_models(state)))
        return Models(available, self._default_model(state, available))

    def _available_models(self, state: dict) -> list[str]:
        # A managed static list replaces discovery outright for the agents that can have one.
        static_models = model_values(state.get(f"{self._tool}_static_models"))
        if static_models:
            return static_models
        return model_values(state.get(f"{self._tool}_models"))

    def _default_model(self, state: dict, available: tuple[str, ...]) -> str | None:
        explicit = state.get(f"{self._tool}_default_model")
        if isinstance(explicit, str) and explicit:
            return explicit
        if not available or self._tool not in _PINS_FIRST_MODEL:
            return None
        return available[0]

    # -- configure ----------------------------------------------------------------------

    def configure(self, state: dict, request: ConfigureRequest) -> dict:
        result = self._write_tool_config(state, request)
        # gemini/copilot/pi return (state, token); codex/claude return state.
        return result[0] if isinstance(result, tuple) else result

    def _write_tool_config(self, state: dict, request: ConfigureRequest) -> dict | tuple[dict, str]:
        write = self._module.write_tool_config
        if self._tool == "codex":
            return write(
                state, request.model, provider=request.provider, parent_schema=request.parent_schema
            )
        if self._tool == "claude":
            # A Model Provider Service or parent schema routes by header and discovers models
            # natively, so the usual "model required" guard doesn't apply to either Claude source.
            if not request.model and not request.provider and not request.parent_schema:
                raise RuntimeError(self._model_required_error)
            extras = request.extras
            return write(
                state,
                request.model,
                provider=request.provider,
                provider_models=extras.get("provider_models"),
                relayed=bool(extras.get("relayed")),
                route_root_model=extras.get("route_root_model"),
                custom_model=extras.get("custom_model"),
                coding_agent_config_defaults=extras.get("coding_agent_config_defaults"),
                parent_schema=request.parent_schema,
                picker_catalog=extras.get("picker_catalog"),
            )
        # Every remaining agent needs a model — including gemini under a provider,
        # which still pins the service's target model in the URL.
        if not request.model:
            raise RuntimeError(self._model_required_error)
        if self._tool == "gemini":
            return write(state, request.model, provider=request.provider)
        return write(state, request.model)

    @property
    def _model_required_error(self) -> str:
        return f"A {self._tool} model must be selected before configuration."

    # -- launch -------------------------------------------------------------------------

    def launch(self, state: dict, args: list[str], *, options: LaunchOptions) -> None:
        self._module.launch(state, args, options=options)

    # -- revert -------------------------------------------------------------------------

    def revert(self, state: dict) -> list[tuple[str, str]]:
        managed = bool((state.get("managed_configs") or {}).get(self._tool))
        spec = self._module.SPEC
        restored = restore_file(spec["config_path"], spec["backup_path"], managed)
        rows = [(f"{self.display} config", "restored" if restored else "unchanged")]
        if self._tool == "codex":
            # Older Codex (< 0.134.0) had ucode edit the shared ~/.codex/config.toml in
            # place; restoring the per-profile file above does not undo that.
            if self._module.revert_legacy_shared_config():
                rows.append((f"{self.display} shared config", "ucode entries removed"))
            rows.append(
                (f"{self.display} OS-managed settings", self._module.revert_managed_config())
            )
        if self._tool == "claude":
            rows.append(
                (f"{self.display} OS-managed settings", self._module.revert_managed_settings())
            )
        return rows


# Published OAuth app a client can register a **native HTTP+OAuth** MCP entry against, keyed by
# client; a client missing here always uses the stdio `ug mcp-proxy`. These clients can pin a
# pre-registered OAuth client and drive the `/oidc` login themselves (so `/mcp` shows "needs
# authentication" / Cursor shows a login), which is a much better experience than the stdio proxy
# for a connection-backed service:
#   - Claude Code: `claude mcp add --transport http --client-id <app>`.
#   - Cursor: a `url` server with an `auth.CLIENT_ID` in ~/.cursor/mcp.json.
# Both need the app *published on the workspace* (ug core checks that per-workspace via
# `oauth_client_available`) and its loopback `/callback` redirect registered on `/oidc` — which
# lacks dynamic client registration, so a pre-registered client is required.
#   - Codex: a `url` server with `oauth.client_id` (`codex mcp add --oauth-client-id
#     --oauth-resource`). Codex derives a per-MCP-server callback path (`/callback/<hash>`) that
#     can't be pre-registered, so `/oidc` accepts it via the
#     `enableCodexLoopbackRedirectExemption` SAFE flag (loopback + codex-cli only).
# Clients whose `mcp add` accepts only a static bearer, not an OAuth client — gemini (`--header`) —
# stay on the stdio proxy even where their apps exist; add one here (with its registration branch in
# `LegacyMcpClient._add_native_http`) once its CLI can pin a client.
_OAUTH_CLIENT_IDS: dict[str, str] = {
    "claude": CLAUDE_CODE_OAUTH_CLIENT_ID,
    "cursor": CURSOR_OAUTH_CLIENT_ID,
    "codex": CODEX_CLI_OAUTH_CLIENT_ID,
}


def _mcp_module() -> ModuleType:
    """:mod:`ucode.mcp`, imported lazily to break the cycle (it imports the agent modules).

    Holds the per-client CLI helpers (`add_codex_mcp_server`, `_gemini_cli_env`, ...) and the
    ``mcp list`` parsers that have not moved into their agent modules yet; the per-agent PRs take
    them, and these call sites go with them."""
    from ucode import mcp

    return mcp


def _user_scope(removed: bool) -> list[str]:
    """``remove``/``add``'s return shape for a client with only one scope: ug writes at user scope."""
    return [MCP_USER_SCOPE] if removed else []


class LegacyMcpClient:
    """One pre-interface MCP client, presented as an :class:`McpClient`.

    Holds the per-client branches ug core used to keep in ``mcp.py``: how each client records a
    server (CLI subprocess vs. config merge), how it removes one, the on-disk entry shape its
    batched write uses, and how its ``mcp list`` reads. Core decides *what* to register (an
    :class:`McpServer`, with the proxy-vs-native-OAuth choice already made); this decides *how*.
    """

    def __init__(self, client: str, module: ModuleType, *, display: str, binary: str) -> None:
        self._client = client
        self._module = module
        self.display = display
        self.binary = binary
        self.oauth_client_id = _OAUTH_CLIENT_IDS.get(client)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LegacyMcpClient({self._client!r})"

    # -- per-server registration --------------------------------------------------------

    def add(self, name: str, server: McpServer) -> list[str]:
        if server.oauth_client is not None:
            removed = self._add_native_http(name, server.url, server.oauth_client)
            if removed is not None:
                return removed
            # No native HTTP+OAuth branch for this client: fall through to the proxy. Unreachable
            # today, since core only sets `oauth_client` for a client with an `oauth_client_id`.
        return self._add_stdio(name, list(server.proxy_argv), always_load=server.always_load)

    def _add_native_http(self, name: str, url: str, client_id: str) -> list[str] | None:
        """Register ``url`` as a direct HTTP server the client drives OAuth against itself, or None
        when this client has no such registration syntax."""
        if self._client == "claude":
            removed_scopes = self._remove_claude_every_scope(name)
            claude.add_claude_http_mcp_server(name, url, client_id=client_id)
            return removed_scopes
        if self._client == "cursor":
            return _user_scope(cursor.write_http_mcp_server_config(name, url, client_id=client_id))
        if self._client == "codex":
            mcp = _mcp_module()
            removed = mcp.remove_codex_mcp_server(name)
            mcp.add_codex_http_mcp_server(name, url, client_id=client_id)
            return _user_scope(removed)
        return None

    def _add_stdio(self, name: str, argv: list[str], *, always_load: bool) -> list[str]:
        """Register the `ug mcp-proxy ...` command as a stdio server; only the syntax differs per
        client. ``always_load`` (the skills registry) is a Claude-only hint to load the server's
        tools at session start; the others don't support it and ignore it."""
        if self._client == "claude":
            removed_scopes = self._remove_claude_every_scope(name)
            claude.add_claude_mcp_server(name, argv, MCP_USER_SCOPE, always_load=always_load)
            return removed_scopes
        if self._client == "codex":
            mcp = _mcp_module()
            removed = mcp.remove_codex_mcp_server(name)
            mcp.add_codex_mcp_server(name, argv)
            return _user_scope(removed)
        if self._client == "gemini":
            mcp = _mcp_module()
            removed = mcp.remove_gemini_mcp_server(name)
            mcp.add_gemini_mcp_server(name, argv)
            return _user_scope(removed)
        # copilot/cursor each merge the entry into their own config file.
        return _user_scope(self._module.write_mcp_server_config(name, argv))

    def remove(self, name: str) -> list[str]:
        if self._client == "claude":
            return self._remove_claude_every_scope(name)
        if self._client == "codex":
            return _user_scope(_mcp_module().remove_codex_mcp_server(name))
        if self._client == "gemini":
            return _user_scope(_mcp_module().remove_gemini_mcp_server(name))
        return _user_scope(self._module.remove_mcp_server_config(name))

    @staticmethod
    def _remove_claude_every_scope(name: str) -> list[str]:
        """Claude keeps MCP servers per scope, so clear every scope ug may have written ``name`` to
        and report the ones it was actually in."""
        return [
            scope for scope in MCP_CLEANUP_SCOPES if claude.remove_claude_mcp_server(name, scope)
        ]

    # -- batched write ------------------------------------------------------------------

    def apply(self, add: Mapping[str, McpServer], remove: set[str]) -> set[str]:
        """Collapse a whole diff into ONE read-modify-write of this client's user-scope config,
        instead of a CLI subprocess or config write per server (see
        ``mcp.apply_mcp_server_changes``)."""
        entries = {name: self.entry(server) for name, server in add.items()}
        return self._module.write_user_mcp_servers(entries, remove) or set()

    def entry(self, server: McpServer) -> dict:
        """The on-disk config entry this client records ``server`` as, matching exactly what its
        per-server CLI/config path (:meth:`add`) would have written."""
        if server.oauth_client is not None:
            native = self._native_http_entry(server.url, server.oauth_client)
            if native is not None:
                return native
        argv = list(server.proxy_argv)
        if self._client == "claude":
            return claude.user_stdio_mcp_entry(argv, always_load=server.always_load)
        if self._client == "codex":
            return codex.managed_mcp_entry(argv)
        return self._module.build_mcp_server_entry(argv)

    def _native_http_entry(self, url: str, client_id: str) -> dict | None:
        if self._client == "claude":
            return claude.managed_mcp_entry(url)
        if self._client == "cursor":
            return cursor.build_http_mcp_server_entry(url, client_id)
        if self._client == "codex":
            return codex.managed_mcp_http_entry(url, client_id)
        return None

    # -- live status --------------------------------------------------------------------

    def live_status(self) -> dict[str, str]:
        output = _mcp_module()._read_mcp_listing([self.binary, "mcp", "list"], env=self._cli_env())
        return self.parse_listing(output) if output is not None else {}

    def parse_listing(self, output: str) -> dict[str, str]:
        """Parse this client's ``mcp list`` output into ``{server_name: live-state}``.

        Parse the rows first, and read the output as "no servers configured" only when none parsed.
        That check looks for phrases like "not found", which also appear inside a single server's
        failure detail (``Failed to connect - HTTP 404 Not Found``), so checking it up front would
        let one broken server empty the whole listing."""
        mcp = _mcp_module()
        # Codex prints a columnar enabled/disabled table; every other client prints health lines.
        parsed = (
            mcp._parse_codex_mcp_list(output)
            if self._client == "codex"
            else mcp._parse_health_mcp_list(output)
        )
        if not parsed and mcp._is_missing_mcp_server_output(output):
            return {}
        return parsed

    def _cli_env(self) -> dict[str, str] | None:
        # Gemini reads its config from a pinned home dir, matching how ug registers servers there.
        return _mcp_module()._gemini_cli_env() if self._client == "gemini" else None


# The MCP clients this adapter wraps. The agents take their display and binary from their own
# `SPEC`; pi is absent because it has no MCP support yet.
LEGACY_MCP_CLIENTS: dict[str, LegacyMcpClient] = {
    client: LegacyMcpClient(
        client,
        LEGACY_MODULES[client],
        display=str(LEGACY_MODULES[client].SPEC["display"]),
        binary=str(LEGACY_MODULES[client].SPEC["binary"]),
    )
    for client in ("claude", "codex", "gemini")
}

# Cursor takes MCP servers but is NOT an `Agent`: it runs models on the user's own Cursor account,
# so ug configures none for it and it stays out of `LEGACY_MODULES`/`AGENTS`. ug core adds it to the
# MCP client registry on its own.
CURSOR_MCP_CLIENT = LegacyMcpClient("cursor", cursor, display="Cursor", binary=cursor.CURSOR_BINARY)


LEGACY_AGENTS: dict[str, LegacyAgent] = {
    tool: LegacyAgent(tool, module) for tool, module in LEGACY_MODULES.items()
}
