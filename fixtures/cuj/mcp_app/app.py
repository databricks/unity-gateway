"""Deterministic MCP fixture service for the Unity Gateway CUJs.

The service deliberately keeps no state.  Every tool derives its response from
the caller supplied run identifier, which makes a test run independently
checkable without putting the expected value in an agent prompt.
"""

from __future__ import annotations

import hashlib
import os
import re

from mcp.server.fastmcp import FastMCP

DEFAULT_PORT = 8000
MAX_RUN_ID_LENGTH = 128
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", re.ASCII)
_DOMAIN_PREFIX = b"unity-gateway-cuj-fixture\0"

PORT = int(os.environ.get("DATABRICKS_APP_PORT", str(DEFAULT_PORT)))

mcp = FastMCP(
    "unity-gateway-cuj-fixture",
    instructions="Stateless deterministic fixtures for Unity Gateway CUJs.",
    host="0.0.0.0",
    port=PORT,
    streamable_http_path="/mcp",
    # The gateway proxy sends each JSON-RPC request independently and does not
    # carry the session id from initialize through later requests.
    stateless_http=True,
)


def _validate_run_id(run_id: str) -> str:
    """Validate and return a bounded, ASCII run identifier."""
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            "run_id must be 1-128 ASCII letters, digits, '_' or '-' and start with a letter or digit"
        )
    return run_id


def _fixture_value(domain: str, run_id: str) -> str:
    """Derive an opaque value with a domain-separated SHA-256 digest."""
    validated_run_id = _validate_run_id(run_id)
    payload = _DOMAIN_PREFIX + domain.encode("ascii") + b"\0" + validated_run_id.encode("ascii")
    return hashlib.sha256(payload).hexdigest()


@mcp.tool()
def read_fixture(run_id: str) -> str:
    """Return the opaque fixture value for a CUJ run.

    Args:
        run_id: A stable test-run identifier (ASCII letters, digits, '_' or '-').
    """
    return _fixture_value("read_fixture", run_id)


@mcp.tool()
def describe_fixture(run_id: str) -> str:
    """Return deterministic opaque metadata for a CUJ run.

    Args:
        run_id: A stable test-run identifier (ASCII letters, digits, '_' or '-').
    """
    return _fixture_value("describe_fixture", run_id)


@mcp.tool()
def decoy_status(run_id: str) -> str:
    """Return a decoy value used to verify tool selector scoping.

    Args:
        run_id: A stable test-run identifier (ASCII letters, digits, '_' or '-').
    """
    return _fixture_value("decoy_status", run_id)


if __name__ == "__main__":
    import uvicorn

    # Avoid an absolute localhost redirect for /mcp and serve the configured
    # path directly through the Databricks Apps reverse proxy.
    app = mcp.streamable_http_app()
    app.router.redirect_slashes = False
    uvicorn.run(app, host="0.0.0.0", port=PORT)
