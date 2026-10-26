"""`ug mcp-proxy`: a stdio MCP server that bridges to a Databricks
streamable-HTTP MCP endpoint, injecting a freshly-minted OAuth bearer.

Every coding agent ucode configures points its Databricks MCP servers at this
one command (``ug mcp-proxy --url <endpoint> --profile <profile>``) as a
local **stdio** server. The agent spawns and reaps the proxy as a child process
— ucode owns no long-lived process and no background refresh thread. The proxy
speaks stdio to the agent and streamable-HTTP to Databricks, and mints a fresh
token from the Databricks CLI profile on **every** upstream HTTP request via an
httpx ``Auth`` hook, so the bearer never goes stale mid-session.

This replaces the previous per-client header auth (static ``Bearer
${OAUTH_TOKEN}``, Claude ``headersHelper``, Cursor literal-token rewrites): one
uniform mechanism, token refresh in a single place, and the proxy is an
invisible implementation detail baked into each client's config.

MCP SDK compatibility (1.x and 2.x). The proxy uses the SDK's *2.x-native* call
shape — ``streamable_http_client(url, http_client=<AsyncClient>)`` — which both
mcp 1.28+ and mcp 2.x export (1.x's older ``streamablehttp_client`` is a thin
deprecated shim over it). The only thing that differs across the major versions
is the HTTP library: mcp 1.x builds on ``httpx``, mcp 2.x on ``httpx2``. We
resolve whichever the installed SDK uses (see ``_httpx``) and build the client
and auth from that, so a single code path works against both — no version cap.

Auth failures are terminal and are reported *fast*. When the Databricks CLI
can't mint a token (expired refresh token, logged-out profile), the proxy prints
the CLI's own message to stderr and exits ``AUTH_FAILURE_EXIT_CODE`` rather than
letting the client wait out its MCP startup timeout. Every server registered
against the same profile fails at once in that state, so a silent hang is
especially confusing -- the user needs to be told to re-run
``databricks auth login``.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import AsyncIterator
from threading import Lock
from types import ModuleType, TracebackType
from typing import Protocol, Self

import anyio
from anyio import to_thread
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCError, JSONRPCNotification, JSONRPCRequest, JSONRPCResponse

from ucode.databricks import (
    ensure_pat_bearer,
    get_databricks_token,
    mcp_service_needs_connection_login,
)
from ucode.mcp_connection_login import connection_from_url, run_connection_login
from ucode.ug_headers import ug_headers

# Exit code used when the proxy cannot continue. MCP clients surface a non-zero
# exit far more usefully than a timeout, so bail out instead of hanging.
AUTH_FAILURE_EXIT_CODE = 2
CONNECTION_AUTH_REQUIRED_ERROR_CODE = -32006
CONNECTION_PERMISSION_DENIED_ERROR_CODE = -32007
CONNECTION_LOGIN_URL_REQUIRED_ERROR_CODE = -32042
AUTHENTICATE_TOOL_NAME = "authenticate"
TOOLS_REFRESH_TIMEOUT_SECONDS = 5.0


def _connection_auth_signal(value: object) -> bool | None:
    """Prefer explicit status/code fields over error text; None means unclassified."""
    if isinstance(value, dict):
        status = value.get("upstream_status", value.get("status_code", value.get("status")))
        if status == 401:
            return True
        if status in (403, 429) or (isinstance(status, int) and status >= 500):
            return False
        code = value.get("code", value.get("error_code"))
        if code in (CONNECTION_AUTH_REQUIRED_ERROR_CODE, CONNECTION_LOGIN_URL_REQUIRED_ERROR_CODE):
            return True
        if code == CONNECTION_PERMISSION_DENIED_ERROR_CODE:
            return False
        for key in ("error", "error_code", "code", "message", "error_description"):
            signal = _connection_auth_signal(value.get(key))
            if signal is not None:
                return signal
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        parsed = None
    if isinstance(parsed, dict):
        return _connection_auth_signal(parsed)
    text = value.casefold().strip()
    if text in ("forbidden", "permission_denied"):
        return False
    if text in ("unauthenticated", "authentication_required", "token_expired", "token_revoked"):
        return True
    if re.search(r"\b(?:invalid_grant|invalid_token)\b", text) or any(
        marker in text
        for marker in (
            "authorization expired or revoked",
            "token has been expired or revoked",
            "token has expired or been revoked",
            "oauth token expired",
            "oauth token has expired",
            "oauth token is expired",
        )
    ):
        return True
    return None


def _connection_auth_required(payload: object) -> bool:
    if isinstance(payload, JSONRPCError):
        if payload.error.code in (
            CONNECTION_AUTH_REQUIRED_ERROR_CODE,
            CONNECTION_LOGIN_URL_REQUIRED_ERROR_CODE,
        ):
            return True
        if payload.error.code == CONNECTION_PERMISSION_DENIED_ERROR_CODE:
            return False
        return (
            _connection_auth_signal({"error": payload.error.data, "message": payload.error.message})
            is True
        )
    if not isinstance(payload, JSONRPCResponse) or payload.result.get("isError") is not True:
        return False
    result = payload.result
    signal = _connection_auth_signal(result.get("structuredContent"))
    if signal is not None:
        return signal
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            signal = _connection_auth_signal(block.get("text"))
            if signal is not None:
                return signal
    return False


def _httpx() -> ModuleType:
    """Return the httpx module the installed MCP SDK is built on.

    mcp 2.x moved from ``httpx`` to ``httpx2`` and passes an ``AsyncClient`` of
    that flavor into ``streamable_http_client``. We must construct our client and
    ``Auth`` from the *same* module the SDK uses, or the transport rejects it.
    Prefer ``httpx2`` (mcp 2.x) and fall back to ``httpx`` (mcp 1.x)."""
    # Imported dynamically: httpx2 ships only with mcp 2.x, so a static `import
    # httpx2` is unresolvable under the mcp 1.x that's pinned for type-checking
    # and CI. importlib keeps the type checker out of it while the runtime picks
    # the right flavor.
    from importlib import import_module

    try:
        return import_module("httpx2")
    except ImportError:
        return import_module("httpx")


class ProxyAuthError(RuntimeError):
    """The proxy could not mint a Databricks token, so it cannot serve requests.

    Kept distinct from a transport error: this one is terminal and actionable
    (the user must re-run `databricks auth login`), so `serve` reports it on
    stderr and exits rather than retrying."""


class ProxyTransportError(RuntimeError):
    """The upstream MCP transport failed or closed unexpectedly."""


class _ReceiveStream[T](Protocol):
    def __aiter__(self) -> AsyncIterator[T]: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None: ...


class _SendStream[T](Protocol):
    async def send(self, item: T, /) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None: ...


def _fail_fast(message: str) -> None:
    """Report a terminal proxy failure on stderr and exit non-zero.

    stdout is the MCP wire, so diagnostics must go to stderr — MCP clients
    surface a child's stderr when it fails to start."""
    print(f"ug mcp-proxy: {message}", file=sys.stderr, flush=True)
    raise SystemExit(AUTH_FAILURE_EXIT_CODE)


class _ConnectionLogin:
    """Share login attempts; permit new recovery after a verified credential is rejected."""

    def __init__(
        self,
        url: str,
        workspace: str,
        profile: str | None,
        *,
        use_pat: bool = False,
    ) -> None:
        self.url = url
        self.workspace = workspace
        self.profile = profile
        self.connection = None if use_pat else connection_from_url(url)
        self._login_attempted = False
        self._completed = False
        self.generation = 0
        self._verified_generation = -1
        self._lock = Lock()

    def record_success(self) -> None:
        self._verified_generation = self.generation

    def run_if_needed(self, rejected_generation: int | None = None) -> bool:
        return self._run_if_needed(
            check_service=True, retry=False, rejected_generation=rejected_generation
        )

    def authenticate_or_confirm_ready(self, rejected_generation: int | None = None) -> bool:
        """Authenticate explicitly, allowing retry after a cancelled or failed login."""
        return self._run_if_needed(
            check_service=False, retry=True, rejected_generation=rejected_generation
        )

    def _run_if_needed(
        self, *, check_service: bool, retry: bool, rejected_generation: int | None
    ) -> bool:
        with self._lock:
            if self.connection is None:
                return False
            if self._completed:
                if rejected_generation is None or rejected_generation < self.generation:
                    return True  # An in-flight request can still report the pre-login failure.
                if not retry and self._verified_generation != self.generation:
                    return False  # A login that never worked must not loop browsers.
                self._completed = False
                self._login_attempted = False
            if check_service:
                try:
                    token = get_databricks_token(self.workspace, self.profile)
                except RuntimeError:
                    return False
                if not mcp_service_needs_connection_login(self.workspace, token, self.connection):
                    return False
            if self._login_attempted and not retry:
                return False
            self._login_attempted = True
            ok, detail = run_connection_login(self.url, self.workspace, profile=self.profile)
            if not ok:
                raise ProxyAuthError(f"connection login for '{self.connection}' failed: {detail}")
            self._completed = True
            self.generation += 1
            return True


def _build_token_auth(
    url: str,
    workspace: str,
    profile: str | None,
    *,
    use_pat: bool = False,
    connection_login: _ConnectionLogin | None = None,
):
    """Build an httpx ``Auth`` that injects a fresh bearer and drives the
    per-connection login **lazily, on a 401** — not eagerly at startup.

    The bearer is the Databricks *workspace* token (read from the CLI session,
    refreshed as it nears expiry). For a connection-backed AI Gateway service,
    the gateway answers with an RFC 9728 ``401`` until the user holds the per-user
    connection credential. Rather than block startup logging in every configured
    server (which pops N browsers and stalls the agent's MCP startup), we let the
    bridge come up immediately and only run
    ``databricks auth login --resource`` when a request actually reports missing
    connection authentication. JSON-RPC authentication errors are handled by the
    bridge below because httpx auth only sees HTTP responses.
    So a browser opens only for a service you actually *use*, one at a time, and
    an already-signed-in service never prompts. The login runs at most once per
    failure episode; PAT profiles have no connection OAuth to drive, so they never do it.

    The base class comes from whichever httpx the SDK uses (see ``_httpx``). Auth
    is async-only: the sync ``auth_flow`` raises, and the async client uses
    ``async_auth_flow``, so the blocking login runs off the event loop."""
    httpx = _httpx()
    # PAT auth has no interactive OAuth to drive, so never treat it as connection-backed.
    connection_login = connection_login or _ConnectionLogin(
        url, workspace, profile, use_pat=use_pat
    )

    def _mint(request):
        # get_databricks_token honors the DATABRICKS_BEARER short-circuit and PAT
        # profiles internally. A RuntimeError means the session is dead (expired
        # refresh token, logged-out profile); raising from inside auth_flow would
        # tear through the transport's task group and stall the process, so
        # translate it into a terminal ProxyAuthError.
        try:
            token = get_databricks_token(workspace, profile)
        except RuntimeError as exc:
            raise ProxyAuthError(str(exc)) from exc
        request.headers["Authorization"] = f"Bearer {token}"

    def _needs_connection_login(response) -> bool:
        # A 401 from a connection-backed service means the per-user credential is
        # missing. Recovery stays bounded until a tool call confirms login worked.
        return response is not None and response.status_code == 401

    class _DatabricksTokenAuth(httpx.Auth):
        def sync_auth_flow(self, request):
            # The proxy always drives this Auth from an httpx AsyncClient (see `_run`), so only
            # `async_auth_flow` is implemented. Fail loudly on the sync entry point so a future sync
            # client can't silently fall back to httpx's no-op default flow (which skips the bearer)
            # instead of injecting the token.
            raise ProxyAuthError("mcp-proxy auth is async-only; use it with an httpx AsyncClient")
            yield request  # unreachable — present only so httpx treats this as a generator

        async def async_auth_flow(self, request):
            generation = connection_login.generation
            _mint(request)
            response = yield request
            if not _needs_connection_login(response):
                return
            # Run the blocking browser login off the event loop so the bridge's
            # other pumps aren't starved while the user completes the sign-in.
            if not await to_thread.run_sync(connection_login.run_if_needed, generation):
                return
            _mint(request)
            yield request

    return _DatabricksTokenAuth()


class _McpBridge:
    """Bridge MCP messages and recover connection authentication failures."""

    def __init__(self, connection_login: _ConnectionLogin) -> None:
        self.connection_login = connection_login
        self.pending_requests: dict[int | str, SessionMessage] = {}
        self.pending_generations: dict[int | str, int] = {}
        self.retried_requests: set[int | str] = set()
        self.authentication_required = False
        self.authentication_completed = False
        self._tools_refresh_waiters: list[anyio.Event] = []

    def _authentication_tool(self) -> dict:
        connection = self.connection_login.connection or "this service"
        return {
            "name": AUTHENTICATE_TOOL_NAME,
            "description": (
                f"Sign in to the Unity Gateway {connection} connection. Call this when the user "
                "requests this service but its normal tools are unavailable. After sign-in, "
                "immediately retry the user's original request in the same turn using the newly "
                "available tools; do not ask the user to send another message."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }

    def _authentication_tool_list_response(self, request_id: int | str) -> SessionMessage:
        return SessionMessage(
            JSONRPCResponse(
                jsonrpc="2.0",
                id=request_id,
                result={"tools": [self._authentication_tool()]},
            )
        )

    @staticmethod
    def _with_tool_list_changes(
        message: SessionMessage,
        original: SessionMessage | None,
    ) -> SessionMessage:
        payload = message.message
        request = original.message if original is not None else None
        if (
            not isinstance(payload, JSONRPCResponse)
            or not isinstance(request, JSONRPCRequest)
            or request.method != "initialize"
        ):
            return message

        result = dict(payload.result)
        capabilities = dict(result.get("capabilities") or {})
        tools = dict(capabilities.get("tools") or {})
        tools["listChanged"] = True
        capabilities["tools"] = tools
        result["capabilities"] = capabilities
        return SessionMessage(
            JSONRPCResponse(jsonrpc="2.0", id=payload.id, result=result),
            metadata=message.metadata,
        )

    def _mark_tools_refreshed(self) -> None:
        waiters, self._tools_refresh_waiters = self._tools_refresh_waiters, []
        for waiter in waiters:
            waiter.set()

    async def _handle_authentication_tool(
        self,
        request_id: int | str,
        client: _SendStream[SessionMessage],
    ) -> None:
        try:
            authenticated = await to_thread.run_sync(
                self.connection_login.authenticate_or_confirm_ready,
                self.connection_login.generation,
            )
        except ProxyAuthError as exc:
            result = {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
        else:
            if not authenticated:
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": "Authentication did not complete. Retry this tool after sign-in.",
                        }
                    ],
                    "isError": True,
                }
            else:
                self.authentication_required = False
                self.authentication_completed = True
                refreshed = anyio.Event()
                self._tools_refresh_waiters.append(refreshed)
                await client.send(
                    SessionMessage(
                        JSONRPCNotification(
                            jsonrpc="2.0",
                            method="notifications/tools/list_changed",
                        )
                    )
                )
                with anyio.move_on_after(TOOLS_REFRESH_TIMEOUT_SECONDS) as scope:
                    await refreshed.wait()
                if refreshed in self._tools_refresh_waiters:
                    self._tools_refresh_waiters.remove(refreshed)
                if self.authentication_required:
                    text = (
                        "Sign-in completed, but this connection still requires authentication. "
                        "Its tools are not ready. Retry authenticate after checking the connection."
                    )
                elif scope.cancel_called:
                    text = (
                        "Authentication complete, but the tool list did not refresh before the "
                        "timeout. Retry the user's original request now. If its tools are still "
                        "unavailable, tell the user to start a new session."
                    )
                else:
                    text = (
                        "Authentication complete. Retry the user's original request now in this "
                        "same turn using the newly available tools. Do not ask the user to send "
                        "another message."
                    )
                result = {
                    "content": [{"type": "text", "text": text}],
                    "isError": self.authentication_required,
                }

        await client.send(
            SessionMessage(JSONRPCResponse(jsonrpc="2.0", id=request_id, result=result))
        )

    async def _authenticate_and_retry(
        self,
        request_id: int | str,
        original: SessionMessage,
        auth_error: SessionMessage,
        client: _SendStream[SessionMessage],
        upstream: _SendStream[SessionMessage],
    ) -> None:
        try:
            authenticated = await to_thread.run_sync(
                self.connection_login.run_if_needed,
                self.pending_generations.get(request_id, 0),
            )
        except ProxyAuthError as exc:
            authenticated = False
            payload = auth_error.message
            if isinstance(payload, JSONRPCError):
                auth_error = SessionMessage(
                    JSONRPCError(
                        jsonrpc="2.0",
                        id=request_id,
                        error=payload.error.model_copy(
                            update={"message": f"{payload.error.message}. {exc}"}
                        ),
                    ),
                    metadata=auth_error.metadata,
                )
            elif isinstance(payload, JSONRPCResponse):
                result = dict(payload.result)
                result["content"] = [
                    *(result.get("content") or []),
                    {"type": "text", "text": str(exc)},
                ]
                auth_error = SessionMessage(
                    JSONRPCResponse(jsonrpc="2.0", id=request_id, result=result),
                    metadata=auth_error.metadata,
                )
        if authenticated:
            self.pending_generations[request_id] = self.connection_login.generation
            await upstream.send(original)
            return
        self.pending_requests.pop(request_id, None)
        self.pending_generations.pop(request_id, None)
        self.retried_requests.discard(request_id)
        self.authentication_required = True
        self.authentication_completed = False
        await client.send(auth_error)
        await client.send(
            SessionMessage(
                JSONRPCNotification(jsonrpc="2.0", method="notifications/tools/list_changed")
            )
        )

    async def pump_client(
        self,
        source: _ReceiveStream[SessionMessage | Exception],
        dest: _SendStream[SessionMessage],
        client: _SendStream[SessionMessage] | None = None,
    ) -> None:
        async with source, dest:
            async with anyio.create_task_group() as tg:
                async for message in source:
                    if isinstance(message, Exception):
                        detail = " ".join(str(message).split()) or type(message).__name__
                        raise ProxyTransportError(
                            f"client MCP transport failed: {detail}"
                        ) from message
                    payload = message.message
                    if isinstance(payload, JSONRPCRequest):
                        if (
                            client is not None
                            and self.authentication_required
                            and payload.method == "tools/call"
                            and isinstance(payload.params, dict)
                            and payload.params.get("name") == AUTHENTICATE_TOOL_NAME
                        ):
                            tg.start_soon(
                                self._handle_authentication_tool,
                                payload.id,
                                client,
                            )
                            continue
                        self.pending_requests[payload.id] = message
                        self.pending_generations[payload.id] = self.connection_login.generation
                    await dest.send(message)

    async def pump_upstream(
        self,
        source: _ReceiveStream[SessionMessage | Exception],
        dest: _SendStream[SessionMessage],
        upstream: _SendStream[SessionMessage],
    ) -> None:
        async with source, dest:
            async with anyio.create_task_group() as tg:
                async for message in source:
                    if isinstance(message, Exception):
                        detail = " ".join(str(message).split()) or type(message).__name__
                        raise ProxyTransportError(
                            f"upstream MCP transport failed: {detail}"
                        ) from message

                    payload = message.message
                    original = None
                    if isinstance(payload, (JSONRPCResponse, JSONRPCError)):
                        original = self.pending_requests.get(payload.id)
                    if self.connection_login.connection is not None:
                        message = self._with_tool_list_changes(message, original)
                    payload = message.message
                    notify_authentication_required = False
                    if isinstance(
                        payload, (JSONRPCError, JSONRPCResponse)
                    ) and _connection_auth_required(payload):
                        request_id = payload.id
                        request = original.message if original is not None else None
                        if (
                            isinstance(request, JSONRPCRequest)
                            and request_id is not None
                            and request.method == "tools/list"
                            and self.connection_login.connection is not None
                        ):
                            self.authentication_required = True
                            self.authentication_completed = False
                            self.pending_requests.pop(request_id, None)
                            self.pending_generations.pop(request_id, None)
                            self.retried_requests.discard(request_id)
                            await dest.send(self._authentication_tool_list_response(request_id))
                            self._mark_tools_refreshed()
                            continue
                        if (
                            isinstance(request, JSONRPCRequest)
                            and request_id is not None
                            and request.method == "tools/call"
                            and request_id not in self.retried_requests
                            and self.connection_login.connection is not None
                        ):
                            self.retried_requests.add(request_id)
                            tg.start_soon(
                                self._authenticate_and_retry,
                                request_id,
                                original,
                                message,
                                dest,
                                upstream,
                            )
                            continue
                        if (
                            isinstance(request, JSONRPCRequest)
                            and request.method == "tools/call"
                            and self.connection_login.connection is not None
                        ):
                            self.authentication_required = True
                            self.authentication_completed = False
                            notify_authentication_required = True

                    tools_refreshed = False
                    if isinstance(payload, JSONRPCResponse) and original is not None:
                        request = original.message
                        if (
                            isinstance(request, JSONRPCRequest)
                            and request.method == "tools/call"
                            and payload.result.get("isError") is not True
                        ):
                            # Some services list tools without checking the user's credential.
                            self.connection_login.record_success()
                            self.authentication_required = False
                        if (
                            isinstance(request, JSONRPCRequest)
                            and request.method == "tools/list"
                            and self.authentication_required
                        ):
                            result = dict(payload.result)
                            result["tools"] = [
                                *(result.get("tools") or []),
                                self._authentication_tool(),
                            ]
                            message = SessionMessage(
                                JSONRPCResponse(jsonrpc="2.0", id=payload.id, result=result),
                                metadata=message.metadata,
                            )
                        tools_refreshed = (
                            isinstance(request, JSONRPCRequest)
                            and request.method == "tools/list"
                            and self.authentication_completed
                        )
                    if (
                        isinstance(payload, (JSONRPCResponse, JSONRPCError))
                        and payload.id is not None
                    ):
                        self.pending_requests.pop(payload.id, None)
                        self.pending_generations.pop(payload.id, None)
                        self.retried_requests.discard(payload.id)
                    await dest.send(message)
                    if notify_authentication_required:
                        await dest.send(
                            SessionMessage(
                                JSONRPCNotification(
                                    jsonrpc="2.0", method="notifications/tools/list_changed"
                                )
                            )
                        )
                    if tools_refreshed:
                        self._mark_tools_refreshed()
        raise ProxyTransportError("upstream MCP transport closed unexpectedly")


async def _run(url: str, workspace: str, profile: str | None, use_pat: bool = False) -> None:
    httpx = _httpx()
    connection_login = _ConnectionLogin(url, workspace, profile, use_pat=use_pat)
    auth = _build_token_auth(
        url,
        workspace,
        profile,
        use_pat=use_pat,
        connection_login=connection_login,
    )
    # 2.x-native shape: hand the transport a pre-built AsyncClient carrying our
    # per-request auth. Works on mcp 1.28+ and 2.x; `streamable_http_client`
    # yields a (read, write) pair in both.
    async with httpx.AsyncClient(
        auth=auth,
        headers=ug_headers(),
        timeout=httpx.Timeout(
            connect=30.0,
            read=300.0,
            write=30.0,
            pool=30.0,
        ),
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            # mcp 1.x yields (read, write, get_session_id); mcp 2.x drops the
            # trailing callback and yields (read, write). Take the first two
            # positionally so both arities work — we don't use get_session_id.
            http_read, http_write = streams[0], streams[1]
            async with stdio_server() as (stdio_read, stdio_write):
                # Bidirectional bridge: client stdin -> Databricks, Databricks -> client stdout.
                bridge = _McpBridge(connection_login)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(bridge.pump_upstream, http_read, stdio_write, http_write)
                    await bridge.pump_client(stdio_read, http_write, stdio_write)
                    tg.cancel_scope.cancel()


def _preflight_token(workspace: str, profile: str | None) -> None:
    """Verify a Databricks token can be minted before opening the bridge.

    Raises ``RuntimeError`` (with the CLI's own message) when auth is dead. This
    is a plain synchronous call: ``get_databricks_token`` already bounds itself
    with subprocess timeouts, so it returns or fails on its own — the point here
    is only to *locate* the failure before the transport starts, where it can be
    reported instead of stalling the session."""
    get_databricks_token(workspace, profile)


def _unwrap_proxy_error(exc: BaseException) -> ProxyAuthError | ProxyTransportError | None:
    """Find a known proxy error in an exception (or ExceptionGroup) tree.

    anyio task groups wrap failures in ExceptionGroups, so failures raised
    inside the transport arrive nested rather than as themselves."""
    if isinstance(exc, (ProxyAuthError, ProxyTransportError)):
        return exc
    for nested in getattr(exc, "exceptions", ()) or ():
        found = _unwrap_proxy_error(nested)
        if found is not None:
            return found
    return None


def serve(url: str, workspace: str, profile: str | None = None, *, use_pat: bool = False) -> None:
    """Run the stdio<->streamable-HTTP MCP proxy until the client closes stdin.

    Authentication is checked up front: a dead profile is a terminal condition,
    and failing here (fast, with the CLI's own message) is far better than
    letting the client wait out its MCP startup timeout with no explanation.

    ``use_pat`` selects static personal-access-token auth: ``databricks auth
    token`` only reads OAuth caches, so a PAT profile's token must be exported as
    ``DATABRICKS_BEARER`` first (``ensure_pat_bearer``) — then every per-request
    mint takes that short-circuit. OAuth needs no such step."""
    if use_pat and not ensure_pat_bearer(profile):
        _fail_fast(
            "--use-pat is set but no personal access token was found for profile "
            f"'{profile or '<none>'}' in ~/.databrickscfg (expected auth_type = pat). "
            "Set DATABRICKS_BEARER, or reconfigure the profile."
        )

    # The per-connection sign-in is NOT driven before opening the bridge. The
    # bridge starts first and drives login only when this service reports missing
    # connection auth, either as HTTP 401 or MCP error -32006. Services whose
    # connections are already authenticated, and services such as web search that
    # need no connection, never open a browser.

    # Pre-flight the token before opening the bridge. Without this, the first
    # token failure surfaces from inside the transport's task group, where it can
    # stall the process instead of erroring out.
    try:
        _preflight_token(workspace, profile)
    except RuntimeError as exc:
        _fail_fast(str(exc))

    try:
        anyio.run(_run, url, workspace, profile, use_pat)
    except BaseException as exc:  # noqa: BLE001 - re-raised unless it's a known proxy failure
        # Errors raised inside the transport arrive wrapped by its task group.
        # Report expected auth/transport failures without hiding programming bugs.
        proxy_error = _unwrap_proxy_error(exc)
        if proxy_error is None:
            raise
        _fail_fast(str(proxy_error))


__all__ = ["AUTH_FAILURE_EXIT_CODE", "ProxyAuthError", "ProxyTransportError", "serve"]
