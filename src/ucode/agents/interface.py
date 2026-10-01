"""The contract between ug core and a coding agent.

ug core (``cli.py``, ``agents/__init__.py``, ``mcp.py``, ...) talks to an agent only through
:class:`Agent`. It never branches on an agent's name for anything every agent does — installing,
configuring, listing models, launching, reverting. Each agent implements those itself, and the
registry in ``agents/__init__.py`` (``AGENTS``) is the one place an agent is listed.

What is deliberately *not* here: features only some agents have (smart routing, tracing, Model
Provider Services, the OS-managed settings file, admin-managed config, Databricks AI Tools). Each of
those keeps its own small table of the agents it supports, next to the feature's code; an agent
missing from such a table simply doesn't have the feature. Adding an agent never touches them, and
adding a feature never touches the agents that lack it.

Rules for growing this interface — a new member must:
  1. be needed for every agent by a user-facing command (configure, launch, status, mcp, revert,
     upgrade/doctor);
  2. have answers that genuinely differ between agents; and
  3. be something core cannot compute itself.
Anything else belongs in a feature table or stays private to the agent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .args import LaunchOptions


@dataclass(frozen=True, kw_only=True)
class Install:
    """Where an agent's CLI comes from and which installed versions ug can drive."""

    binary: str
    # npm package spec for `npm install -g` (install, and upgrade when there is no native updater).
    package: str
    # The agent's own self-updater (`claude upgrade`), used instead of npm when the binary exists.
    upgrade_argv: tuple[str, ...] | None = None
    # Returns a blocking message when the installed version is too old, else None.
    version_error: Callable[[], str | None] | None = None
    # Returns (installed, downgrade_target) when the installed version is too new, else None.
    too_new: Callable[[], tuple[str, str] | None] | None = None
    # Runs before ug installs or upgrades the binary (e.g. detach version-specific metadata).
    before_install: Callable[[], None] | None = None


@dataclass(frozen=True)
class Models:
    """The models an agent can use given the discovered workspace inventory in ``state``."""

    available: tuple[str, ...]
    # The model ug pins as the starting model, or None when the agent chooses its own.
    default: str | None


@dataclass(frozen=True, kw_only=True)
class ConfigureRequest:
    """Inputs to :meth:`Agent.configure`. Agents ignore fields for features they don't support."""

    # The model to configure, or None to let the agent pick from what ``state`` makes available.
    model: str | None = None
    # A Model Provider Service to route through (agents listed in the provider-service table).
    provider: str | None = None
    # A Unity Catalog model location to discover models from (agents that support one).
    parent_schema: str | None = None
    # Launch-time inputs only Claude reads today (provider_models, relayed, route_root_model,
    # custom_model, coding_agent_config_defaults, picker_catalog). Computed by cli.py's launch path;
    # goes away once that planning moves into claude.py.
    extras: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class McpServer:
    """One MCP server to register, with its delivery already chosen by ug core."""

    url: str
    # The `ug mcp-proxy ...` stdio command, which injects a fresh Databricks token. Used unless
    # `oauth_client` is set.
    proxy_argv: tuple[str, ...]
    # Register natively over HTTP+OAuth with this published app id instead of the proxy. Core sets
    # it only for a client with an `oauth_client_id`, on a connection-backed service.
    oauth_client: str | None = None
    # Load the server's tools at session start (only Claude honours it; others ignore it).
    always_load: bool = False


@runtime_checkable
class McpClient(Protocol):
    """Something ug can register MCP servers into: an agent's ``mcp``, or an MCP-only client."""

    display: str
    binary: str
    # Published OAuth app id for native HTTP+OAuth entries, or None to always use the proxy.
    oauth_client_id: str | None

    def add(self, name: str, server: McpServer) -> list[str]:
        """Register (or replace) one server; return the scopes an existing entry was removed from."""
        ...

    def remove(self, name: str) -> list[str]:
        """Remove one server; return the scopes it was removed from (empty if it wasn't there)."""
        ...

    def apply(self, add: Mapping[str, McpServer], remove: set[str]) -> set[str]:
        """Apply many adds and removes in one write; return the names actually removed."""
        ...

    def live_status(self) -> dict[str, str]:
        """``{server_name: state}`` from the client's own listing; empty when it can't be read."""
        ...


@runtime_checkable
class Agent(Protocol):
    """A coding agent ug configures and launches through Databricks AI Gateway."""

    # Human-readable name for messages ("Claude Code", "GitHub Copilot CLI").
    display: str
    install: Install
    # Where ug registers MCP servers for this agent, or None when the agent can't receive them.
    # Required (no default) so "doesn't support MCP" is always an explicit choice.
    mcp: McpClient | None

    def models(self, state: dict) -> Models:
        """What this agent can use from the workspace inventory in ``state``.

        Drives availability checks, ``ug status``, and model pickers. Must not mutate ``state``."""
        ...

    def configure(self, state: dict, request: ConfigureRequest) -> dict:
        """Write the agent's ug-owned config and return the updated state.

        Raises RuntimeError with an actionable message when the agent can't be configured."""
        ...

    def launch(self, state: dict, args: list[str], *, options: LaunchOptions) -> None:
        """Hand the terminal to the agent. Does not return normally (exec or SystemExit)."""
        ...

    def revert(self, state: dict) -> list[tuple[str, str]]:
        """Restore or remove what ug wrote for this agent (MCP entries excluded).

        Returns ``(label, outcome)`` rows for the ``ug revert`` summary, e.g.
        ``("Pi settings", "restored")``."""
        ...
