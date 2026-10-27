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
import time
from collections.abc import AsyncIterator
from threading import Lock
from types import ModuleType, TracebackType
from typing import Protocol, Self
from urllib.parse import parse_qs, urlsplit

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
    workspace_origin,
)
from ucode.mcp_connection_login import connection_from_url, run_connection_login
from ucode.ug_headers import ug_headers

# Exit code used when the proxy cannot continue. MCP clients surface a non-zero
# exit far more usefully than a timeout, so bail out instead of hanging.
AUTH_FAILURE_EXIT_CODE = 2
CONNECTION_AUTH_REQUIRED_ERROR_CODE = -32006
URL_ELICITATION_REQUIRED_ERROR_CODE = -32042
AUTHENTICATE_TOOL_NAME = "authenticate"
TOOLS_REFRESH_TIMEOUT_SECONDS = 5.0
WORKSPACE_REFRESH_INTERVAL_SECONDS = 30.0
METADATA_RETRY_INTERVAL_SECONDS = 5.0


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
    """Run automatic login once per proxy session; explicit login may retry."""

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
        self._login_supported: bool | None = None
        self._metadata_retry_at = 0.0
        self._metadata_lock = Lock()
        self._lock = Lock()

    def supports_login(self, *, force: bool = False) -> bool:
        """Check connection metadata without opening a browser."""
        if self.connection is None:
            return False
        with self._metadata_lock:
            if self._login_supported is None:
                if not force and time.monotonic() < self._metadata_retry_at:
                    return False
                try:
                    token = get_databricks_token(self.workspace, self.profile)
                except RuntimeError:
                    self._metadata_retry_at = time.monotonic() + METADATA_RETRY_INTERVAL_SECONDS
                    return False
                self._login_supported = mcp_service_needs_connection_login(
                    self.workspace, token, self.connection
                )
                if self._login_supported is None:
                    self._metadata_retry_at = time.monotonic() + METADATA_RETRY_INTERVAL_SECONDS
            return self._login_supported is True

    def is_login_url(self, url: str) -> bool:
        if self.connection is None:
            return False
        try:
            parsed = urlsplit(url)
            origin = urlsplit(workspace_origin(self.workspace))
            return (
                parsed.scheme == "https"
                and parsed.netloc.lower() == origin.netloc.lower()
                and parsed.path == "/mcp-service-login"
                and not parsed.fragment
                and parse_qs(parsed.query).get("name") == [self.connection]
            )
        except (ValueError, RuntimeError):
            return False

    def run_if_needed(self) -> bool:
        """Run automatic connection login once, returning whether the connection is ready."""
        return self._run_if_needed(check_service=True, retry=False)

    def authenticate_or_confirm_ready(self) -> bool:
        """Authenticate explicitly, including after earlier success or cancellation."""
        return self._run_if_needed(check_service=False, retry=True)

    def _run_if_needed(self, *, check_service: bool, retry: bool) -> bool:
        with self._lock:
            if self.connection is None:
                return False
            if self._completed and not retry:
                return True
            if check_service and not self.supports_login(force=True):
                return False
            if self._login_attempted and not retry:
                return False
            self._login_attempted = True
            self._completed = False
            ok, detail = run_connection_login(self.url, self.workspace, profile=self.profile)
            if not ok:
                raise ProxyAuthError(f"connection login for '{self.connection}' failed: {detail}")
            self._completed = True
            return True


def _build_token_auth(
    url: str,
    workspace: str,
    profile: str | None,
    *,
    use_pat: bool = False,
    connection_login: _ConnectionLogin | None = None,
):
    """Inject workspace auth; persistent tool-call 401s may trigger connection login.

    Initialization and discovery must never open connector login. A 401 first
    retries with a silently refreshed workspace token. Only an actual tools/call
    still returning 401 can drive per-user sign-in, once per proxy session.
    JSON-RPC authentication errors are handled by the bridge below.
    """
    httpx = _httpx()
    # PAT auth has no interactive OAuth to drive, so never treat it as connection-backed.
    connection_login = connection_login or _ConnectionLogin(
        url, workspace, profile, use_pat=use_pat
    )

    def _mint(request, force_refresh=False):
        # get_databricks_token honors the DATABRICKS_BEARER short-circuit and PAT
        # profiles internally. A RuntimeError means the session is dead (expired
        # refresh token, logged-out profile); raising from inside auth_flow would
        # tear through the transport's task group and stall the process, so
        # translate it into a terminal ProxyAuthError.
        try:
            if force_refresh:
                token = get_databricks_token(workspace, profile, force_refresh=True)
            else:
                token = get_databricks_token(workspace, profile)
        except RuntimeError as exc:
            raise ProxyAuthError(str(exc)) from exc
        request.headers["Authorization"] = f"Bearer {token}"

    def _is_unauthorized(response) -> bool:
        return response is not None and response.status_code == 401

    class _DatabricksTokenAuth(httpx.Auth):
        def __init__(self):
            self._refresh_lock = anyio.Lock()
            self._refresh_at = 0.0
            self._refresh_failed = False

        def sync_auth_flow(self, request):
            # The proxy always drives this Auth from an httpx AsyncClient (see `_run`), so only
            # `async_auth_flow` is implemented. Fail loudly on the sync entry point so a future sync
            # client can't silently fall back to httpx's no-op default flow (which skips the bearer)
            # instead of injecting the token.
            raise ProxyAuthError("mcp-proxy auth is async-only; use it with an httpx AsyncClient")
            yield request  # unreachable — present only so httpx treats this as a generator

        async def async_auth_flow(self, request):
            await request.aread()
            _mint(request)
            response = yield request
            if not _is_unauthorized(response) or use_pat:
                return
            authorization = request.headers["Authorization"]
            async with self._refresh_lock:
                refresh = time.monotonic() >= self._refresh_at
                if refresh:
                    self._refresh_at = time.monotonic() + WORKSPACE_REFRESH_INTERVAL_SECONDS
                elif self._refresh_failed:
                    return
                try:
                    await to_thread.run_sync(_mint, request, refresh)
                except ProxyAuthError:
                    self._refresh_failed = True
                    return
                self._refresh_failed = False
            if refresh or request.headers["Authorization"] != authorization:
                response = yield request
            if not _is_unauthorized(response) or request.method != "POST":
                return
            try:
                payload = json.loads(request.content)
            except ValueError:
                return
            if (
                not isinstance(payload, dict)
                or payload.get("method") != "tools/call"
                or payload.get("id") is None
            ):
                return
            # Run the blocking browser login off the event loop so the bridge's
            # other pumps aren't starved while the user completes the sign-in.
            if not await to_thread.run_sync(connection_login.run_if_needed):
                return
            _mint(request)
            yield request

    return _DatabricksTokenAuth()


class _McpBridge:
    """Bridge MCP messages and recover connection authentication failures."""

    def __init__(self, connection_login: _ConnectionLogin) -> None:
        self.connection_login = connection_login
        self.pending_requests: dict[int | str, SessionMessage] = {}
        self.retried_requests: set[int | str] = set()
        self.authentication_required = False
        self.authentication_completed = False
        self.authentication_tool_available = False
        self._upstream_authentication_tool = False
        self._tools_refresh_waiters: list[anyio.Event] = []

    def _is_connection_auth_error(self, payload: JSONRPCResponse | JSONRPCError) -> bool:
        if self.connection_login.connection is None:
            return False
        if isinstance(payload, JSONRPCError):
            if payload.error.code == CONNECTION_AUTH_REQUIRED_ERROR_CODE:
                return True
            if payload.error.code != URL_ELICITATION_REQUIRED_ERROR_CODE:
                return False
            data = payload.error.data
            elicitations = data.get("elicitations") if isinstance(data, dict) else None
            # Mixed challenges may include policy approval that login must not replace.
            return (
                isinstance(elicitations, list)
                and bool(elicitations)
                and all(
                    isinstance(item, dict)
                    and item.get("mode") == "url"
                    and isinstance(item.get("url"), str)
                    and self.connection_login.is_login_url(item["url"])
                    for item in elicitations
                )
            )
        if payload.result.get("isError") is not True:
            return False
        content = payload.result.get("content")
        if not isinstance(content, list):
            return False
        return any(
            self.connection_login.is_login_url(url.rstrip(".,;)"))
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            for url in re.findall(r"https://[^\s<>\"'`]+", item["text"])
        )

    def _authentication_tool(self) -> dict:
        connection = self.connection_login.connection or "this service"
        return {
            "name": AUTHENTICATE_TOOL_NAME,
            "description": (
                f"Sign in to the Unity Gateway {connection} connection. Call this when the user "
                "requests this service and its tools report missing or expired sign-in, or "
                "authentication is required to discover its tools. For an access denial, "
                "including an IP-address denial, try sign-in once before recommending "
                "administrator or network changes. If the denial persists after sign-in and "
                "retry, report the original error and stop retrying authentication. Do not use "
                "this for other generic tool failures. After sign-in, "
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

    async def _with_authentication_tool(self, message: SessionMessage) -> SessionMessage:
        payload = message.message
        if not isinstance(payload, JSONRPCResponse):
            return message
        tools = payload.result.get("tools")
        if not isinstance(tools, list):
            return message
        if any(
            isinstance(tool, dict) and tool.get("name") == AUTHENTICATE_TOOL_NAME for tool in tools
        ):
            self._upstream_authentication_tool = True
            self.authentication_tool_available = False
        if self._upstream_authentication_tool or payload.result.get("nextCursor"):
            return message
        if not await to_thread.run_sync(self.connection_login.supports_login):
            return message
        self.authentication_tool_available = True
        return SessionMessage(
            JSONRPCResponse(
                jsonrpc="2.0",
                id=payload.id,
                result={**payload.result, "tools": [*tools, self._authentication_tool()]},
            ),
            metadata=message.metadata,
        )

    async def _send_tool_list_response(
        self, message: SessionMessage, client: _SendStream[SessionMessage]
    ) -> None:
        payload = message.message
        if (
            isinstance(payload, (JSONRPCResponse, JSONRPCError))
            and payload.id is not None
            and self._is_connection_auth_error(payload)
            and not self._upstream_authentication_tool
        ):
            connection_error = (
                isinstance(payload, JSONRPCError)
                and payload.error.code == CONNECTION_AUTH_REQUIRED_ERROR_CODE
            )
            if not connection_error and not await to_thread.run_sync(
                self.connection_login.supports_login
            ):
                await client.send(message)
                return
            self.authentication_required = True
            self.authentication_completed = False
            self.authentication_tool_available = True
            response = self._authentication_tool_list_response(payload.id)
            response.metadata = message.metadata
            await client.send(response)
            self._mark_tools_refreshed()
            return
        await client.send(await self._with_authentication_tool(message))
        if isinstance(payload, JSONRPCResponse) and self.authentication_completed:
            self._mark_tools_refreshed()

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
                self.connection_login.authenticate_or_confirm_ready
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
                if self.authentication_required:
                    text = (
                        "Sign-in finished, but this service still requires authentication. "
                        "Retry the authenticate tool to sign in again."
                    )
                elif scope.cancel_called:
                    if refreshed in self._tools_refresh_waiters:
                        self._tools_refresh_waiters.remove(refreshed)
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
            authenticated = await to_thread.run_sync(self.connection_login.run_if_needed)
        except ProxyAuthError:
            authenticated = False
        if authenticated:
            await upstream.send(original)
            return
        self.pending_requests.pop(request_id, None)
        self.retried_requests.discard(request_id)
        await client.send(auth_error)

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
                            and (self.authentication_required or self.authentication_tool_available)
                            and not self._upstream_authentication_tool
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
                    if isinstance(payload, (JSONRPCResponse, JSONRPCError)):
                        request_id = payload.id
                        request = original.message if original is not None else None
                        if (
                            isinstance(request, JSONRPCRequest)
                            and request_id is not None
                            and request.method == "tools/call"
                            and not self._upstream_authentication_tool
                            and request_id not in self.retried_requests
                            and self._is_connection_auth_error(payload)
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

                    if isinstance(payload, (JSONRPCResponse, JSONRPCError)):
                        self.pending_requests.pop(payload.id, None)
                        self.retried_requests.discard(payload.id)
                    request = original.message if original is not None else None
                    if (
                        isinstance(payload, (JSONRPCResponse, JSONRPCError))
                        and isinstance(request, JSONRPCRequest)
                        and request.method == "tools/list"
                    ):
                        tg.start_soon(self._send_tool_list_response, message, dest)
                        continue
                    await dest.send(message)
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
    # connection auth, via HTTP 401, MCP auth errors, or a service-scoped login URL. Services whose
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
