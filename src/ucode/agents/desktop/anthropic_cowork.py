"""Relay-only Claude Docs connector and account-skill integration."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

from ucode.desktop_auth import get_claude_subscription_token
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)

PLUGIN_NAME = "ug-account-docs"
PLUGIN_ID = f"{PLUGIN_NAME}@unity-gateway-cowork"
CONNECTOR_NAME = "claude-docs"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


@dataclass
class DocsResources:
    server_id: str
    files: dict[str, bytes]
    claude_binary: str
    config_dir: str


@contextmanager
def credential_source(config_dir: str) -> Iterator[None]:
    keys = ("CLAUDE_CONFIG_DIR", "CLAUDE_HELPER_CONTEXT")
    previous = {key: os.environ.get(key) for key in keys}
    if config_dir:
        os.environ["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ["CLAUDE_HELPER_CONTEXT"] = "background"
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class AccountClient:
    def __init__(
        self,
        claude_binary: str,
        *,
        token_provider: Callable[[], str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.token_provider = token_provider or (
            lambda: get_claude_subscription_token(claude_binary)
        )
        self.client = httpx.Client(
            base_url="https://api.anthropic.com",
            timeout=10,
            follow_redirects=False,
            transport=transport,
            headers={
                "anthropic-version": "2023-06-01",
                "User-Agent": "unity-gateway-cowork",
            },
        )

    def __enter__(self) -> AccountClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.client.close()

    def get(self, path: str, *, org: str | None = None, connectors: bool = False) -> bytes:
        if not path.startswith("/") or path.startswith("//"):
            raise RuntimeError("Anthropic account requests require a relative API path.")
        headers = {"Authorization": f"Bearer {self.token_provider()}"}
        if org:
            headers["x-organization-uuid"] = org
        if connectors:
            headers["anthropic-beta"] = "mcp-servers-2025-12-04"
        try:
            with self.client.stream("GET", path, headers=headers) as response:
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Anthropic account API returned HTTP {response.status_code}."
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_ARCHIVE_BYTES:
                        raise RuntimeError("Anthropic account response exceeded 32 MiB.")
                return bytes(body)
        except httpx.HTTPError:
            raise RuntimeError("Could not reach the Anthropic account API.") from None

    def json(self, path: str, **kwargs) -> dict:
        try:
            result = json.loads(self.get(path, **kwargs))
        except (ValueError, TypeError):
            raise RuntimeError("Anthropic account API returned invalid JSON.") from None
        if not isinstance(result, dict):
            raise RuntimeError("Anthropic account API returned an unexpected response.")
        return result

    def docs_skill(self) -> dict[str, bytes] | None:
        profile = self.json("/api/oauth/profile")
        org = (profile.get("organization") or {}).get("uuid")
        if not isinstance(org, str) or not org:
            raise RuntimeError("Anthropic account profile has no organization UUID.")
        path = f"/api/oauth/organizations/{quote(org, safe='')}/skills"
        skills = self.json(f"{path}/list-skills?include_wiggle_skills=true", org=org)
        if not isinstance(skills.get("skills"), list):
            raise RuntimeError("Anthropic account API returned no skills array.")
        if not any(
            s.get("id") == "docs" and s.get("enabled") is not False for s in skills["skills"]
        ):
            return None
        return unpack_docs_skill(self.get(f"{path}/docs/download", org=org))

    def docs_connector(self) -> str:
        result = self.json(
            "/v1/mcp_servers?limit=1000&include_additional_installs=true", connectors=True
        )
        if not isinstance(result.get("data"), list):
            raise RuntimeError("Anthropic account API returned no connector array.")
        for server in result["data"]:
            if str(server.get("display_name", "")).casefold() != "claude docs":
                continue
            if server.get("eligible") is not True:
                raise RuntimeError("Claude Docs connector is not eligible for this account.")
            server_id = server.get("id")
            if isinstance(server_id, str) and server_id:
                return server_id
        raise RuntimeError("Claude Docs connector is not connected to this account.")


def unpack_docs_skill(body: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            files: dict[str, bytes] = {}
            total = 0
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or ":" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise RuntimeError("Anthropic skill archive contains an unsafe path.")
                if entry.is_dir():
                    continue
                parts = path.parts[1:] if path.parts[:1] == ("docs",) else path.parts
                if not parts or any(part.startswith(".") for part in parts):
                    raise RuntimeError("Anthropic skill archive contains an unsafe path.")
                relative = PurePosixPath(*parts).as_posix()
                total += entry.file_size
                if total > MAX_ARCHIVE_BYTES or len(files) >= 512 or relative in files:
                    raise RuntimeError(
                        "Anthropic skill archive exceeds limits or has duplicate files."
                    )
                files[relative] = archive.read(entry)
            markdown = files.get("SKILL.md", b"").decode("utf-8")
            if not markdown.startswith("---"):
                raise RuntimeError("Anthropic docs archive has no valid SKILL.md.")
            return files
    except (zipfile.BadZipFile, UnicodeError, OSError):
        raise RuntimeError("Anthropic docs download was not a readable skill archive.") from None


def load_resources(claude_binary: str) -> DocsResources:
    with AccountClient(claude_binary) as client:
        server_id = client.docs_connector()
        files = client.docs_skill()
    return DocsResources(
        server_id, files or {}, claude_binary, os.environ.get("CLAUDE_CONFIG_DIR", "")
    )


def write_skill(destination: Path, files: dict[str, bytes]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def write_plugin(root: Path, resources: DocsResources, executable: str, workspace: str) -> None:
    def write_json(relative: str, value: object) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    auth_args = [
        "--claude-binary",
        resources.claude_binary,
        "--claude-config-dir",
        resources.config_dir,
    ]
    write_json(
        ".claude-plugin/plugin.json",
        {
            "name": PLUGIN_NAME,
            "author": {"name": "Databricks"},
            "description": "Claude Docs and its account skill through Anthropic relay authentication",
            "mcpServers": "./.mcp.json",
            "skills": "./skills",
        },
    )
    write_json(
        ".mcp.json",
        {
            "mcpServers": {
                CONNECTOR_NAME: {
                    "type": "stdio",
                    "command": executable,
                    "args": [
                        "-m",
                        "ucode.cli",
                        "anthropic-mcp-proxy",
                        "--host",
                        workspace,
                        "--server-id",
                        resources.server_id,
                        *auth_args,
                    ],
                }
            }
        },
    )
    argv = [
        executable,
        "-m",
        "ucode.cli",
        "cowork-hook",
        "--host",
        workspace,
        "--anthropic",
        *auth_args,
    ]
    write_json(
        "hooks/hooks.json",
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": subprocess_cross_os.shell_command(argv),
                                "timeout": 60,
                            }
                        ]
                    }
                ]
            }
        },
    )
    (root / "skills").mkdir(parents=True, exist_ok=True)
    if resources.files:
        write_skill(root / "skills" / "docs", resources.files)


def sync_hook(payload: object, root: Path, claude_binary: str) -> dict:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
        return {}
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    if manifest.get("name") != PLUGIN_NAME:
        raise RuntimeError("Anthropic hook received another plugin's directory.")
    with (root / ".docs-sync.lock").open("a+b") as lock:
        try:
            acquire_exclusive_file_lock(lock, timeout=5)
        except TimeoutError:
            raise RuntimeError("Another Cowork session is refreshing the docs skill.") from None
        try:
            return _sync_docs_skill(root, claude_binary)
        finally:
            release_file_lock(lock)


def _sync_docs_skill(root: Path, claude_binary: str) -> dict:
    with AccountClient(claude_binary) as client:
        files = client.docs_skill()
    skills = root / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    destination = skills / "docs"
    staging = Path(tempfile.mkdtemp(prefix=".docs-", dir=root))
    backup = root / ".docs-previous"
    try:
        write_skill(staging, files or {})
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            os.replace(destination, backup)
        try:
            if files:
                os.replace(staging, destination)
        except OSError:
            if backup.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "reloadSkills": True,
            "additionalContext": (
                "Anthropic Claude Docs is configured through the ug-account-docs plugin. "
                "Use its exposed MCP tools; tool names have the plugin_ug-account-docs prefix. "
                + (
                    "Load its docs skill before document operations."
                    if files
                    else "The account docs skill is disabled or unavailable."
                )
            ),
        }
    }
