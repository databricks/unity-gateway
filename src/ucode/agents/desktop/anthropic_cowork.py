"""Relay-only Anthropic account connectors and skills."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

from ucode import config_io
from ucode.desktop_auth import get_claude_subscription_token
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)
from ucode.ui import print_warning

PLUGIN_NAME = "ug-account-docs"
PLUGIN_ID = f"{PLUGIN_NAME}@unity-gateway-cowork"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
SKILL_MANIFEST = ".account-skills.json"


@dataclass
class SkillFile:
    content: bytes
    executable: bool = False


@dataclass
class AccountSkill:
    id: str
    name: str
    files: dict[str, SkillFile] | None


@dataclass
class AccountResources:
    connectors: dict[str, str]
    skills: list[AccountSkill]
    org: str
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

    def skills(self) -> tuple[str, list[AccountSkill]]:
        profile = self.json("/api/oauth/profile")
        organization = profile.get("organization")
        org = organization.get("uuid") if isinstance(organization, dict) else None
        if not isinstance(org, str) or not org:
            raise RuntimeError("Anthropic account profile has no organization UUID.")
        path = f"/api/oauth/organizations/{quote(org, safe='')}/skills"
        result = self.json(f"{path}/list-skills?include_wiggle_skills=true", org=org)
        catalog = _catalog(result, "skills")
        enabled = [s for s in catalog if s.get("enabled") is True]
        aliases = _aliases(enabled, "name")

        def download(skill: dict) -> AccountSkill:
            skill_id = skill["id"]
            name = aliases[skill_id]
            try:
                files = unpack_skill(
                    self.get(f"{path}/{quote(skill_id, safe='')}/download", org=org)
                )
            except RuntimeError as exc:
                print_warning(f"Could not download Anthropic skill {name}: {exc}")
                files = None
            return AccountSkill(skill_id, name, files)

        with ThreadPoolExecutor(max_workers=4) as pool:
            return org, list(pool.map(download, enabled))

    def connectors(self) -> dict[str, str]:
        result = self.json(
            "/v1/mcp_servers?limit=1000&include_additional_installs=true", connectors=True
        )
        servers = _catalog(result, "data")
        eligible = [server for server in servers if server.get("eligible") is True]
        return {name: server_id for server_id, name in _aliases(eligible, "display_name").items()}


def _catalog(result: dict, key: str) -> list[dict]:
    items = result.get(key)
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise RuntimeError(f"Anthropic account API returned an invalid {key} array.")
    if result.get("has_more") or result.get("next_cursor") or result.get("nextCursor"):
        raise RuntimeError("Anthropic account listing was incomplete; keeping existing resources.")
    return items


def _aliases(items: list[dict], name_key: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise RuntimeError("Anthropic account resource has no valid ID.")
        label = item.get(name_key) or item_id
        name = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")[:48] or "resource"
        names[item_id] = name
    counts = Counter(names.values())
    used: set[str] = set()
    for item_id, name in sorted(names.items()):
        if counts[name] > 1:
            name += "-" + hashlib.sha256(item_id.encode()).hexdigest()[:12]
        if name in used:
            raise RuntimeError("Anthropic account resources have conflicting names.")
        names[item_id] = name
        used.add(name)
    return names


def unpack_skill(body: bytes) -> dict[str, SkillFile]:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            files: dict[str, SkillFile] = {}
            paths: set[str] = set()
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
                parts = path.parts
                if not parts or any(part.startswith(".") for part in parts):
                    raise RuntimeError("Anthropic skill archive contains an unsafe path.")
                relative = PurePosixPath(*parts).as_posix()
                total += entry.file_size
                folded = relative.casefold()
                if total > MAX_ARCHIVE_BYTES or len(files) >= 512 or folded in paths:
                    raise RuntimeError(
                        "Anthropic skill archive exceeds limits or has duplicate files."
                    )
                paths.add(folded)
                files[relative] = SkillFile(
                    archive.read(entry), bool((entry.external_attr >> 16) & 0o111)
                )
            if any(
                parent.as_posix().casefold() in paths
                for name in files
                for parent in PurePosixPath(name).parents
                if parent.as_posix() != "."
            ):
                raise RuntimeError("Anthropic skill archive has conflicting file paths.")
            if "SKILL.md" not in files:
                roots = {name.split("/", 1)[0] for name in files}
                if len(roots) != 1:
                    raise RuntimeError("Anthropic archive has no single skill root.")
                prefix = next(iter(roots)) + "/"
                files = {name.removeprefix(prefix): content for name, content in files.items()}
            markdown = files.get("SKILL.md", SkillFile(b"")).content.decode("utf-8")
            if not markdown.startswith("---"):
                raise RuntimeError("Anthropic archive has no valid SKILL.md.")
            return files
    except (zipfile.BadZipFile, UnicodeError, OSError, ValueError, NotImplementedError):
        raise RuntimeError("Anthropic download was not a readable skill archive.") from None


def load_resources(claude_binary: str) -> AccountResources:
    with AccountClient(claude_binary) as client:
        connectors = client.connectors()
        org, skills = client.skills()
    return AccountResources(
        connectors, skills, org, claude_binary, os.environ.get("CLAUDE_CONFIG_DIR", "")
    )


def write_skill(destination: Path, files: dict[str, SkillFile]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, file in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file.content)
        if file.executable:
            path.chmod(0o755)


def write_plugin(
    root: Path,
    resources: AccountResources,
    executable: str,
    workspace: str,
    *,
    previous_root: Path | None = None,
) -> None:
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
            "description": "Anthropic account connectors and enabled skills through relay authentication",
            "mcpServers": "./.mcp.json",
            "skills": "./skills",
        },
    )
    write_json(
        ".mcp.json",
        {
            "mcpServers": {
                name: {
                    "type": "stdio",
                    "command": executable,
                    "args": [
                        "-m",
                        "ucode.cli",
                        "anthropic-mcp-proxy",
                        "--host",
                        workspace,
                        "--server-id",
                        server_id,
                        *auth_args,
                    ],
                }
                for name, server_id in resources.connectors.items()
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
                                "timeout": 180,
                            }
                        ]
                    }
                ]
            }
        },
    )
    _write_skills(root, resources.org, resources.skills, previous_root or root)


def sync_hook(payload: object, root: Path, claude_binary: str) -> dict:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
        return {}
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    if manifest.get("name") != PLUGIN_NAME:
        raise RuntimeError("Anthropic hook received another plugin's directory.")
    with (root / ".account-sync.lock").open("a+b") as lock:
        try:
            acquire_exclusive_file_lock(lock, timeout=5)
        except TimeoutError:
            raise RuntimeError("Another Cowork session is refreshing account skills.") from None
        try:
            return _sync_skills(root, claude_binary)
        finally:
            release_file_lock(lock)


def _sync_skills(root: Path, claude_binary: str) -> dict:
    with AccountClient(claude_binary) as client:
        org, skills = client.skills()
    staging = Path(tempfile.mkdtemp(prefix=".account-", dir=root))
    destination = root / "skills"
    backup = root / ".account-previous"
    try:
        _write_skills(staging, org, skills, root)
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(staging / "skills", destination)
            os.replace(staging / SKILL_MANIFEST, root / SKILL_MANIFEST)
        except OSError:
            if destination.exists():
                shutil.rmtree(destination)
            if backup.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    connectors = config_io.read_json_safe(root / ".mcp.json").get("mcpServers", {})
    failed = [skill.name for skill in skills if skill.files is None]
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "reloadSkills": True,
            "additionalContext": (
                "Anthropic account resources use the ug-account-docs plugin namespace; "
                "workspace system.ai connectors use the separate ug plugin. "
                "Load relevant account skills before using their connectors. "
                f"Configured account connector aliases: {', '.join(connectors) or 'none'}. "
                "Imported skills do not provide missing native browser, artifact, or computer tools. "
                "Connector installation changes require ug configure and a new Cowork task."
                + (f" Account skill downloads unavailable: {', '.join(failed)}." if failed else "")
            ),
        }
    }


def _write_skills(root: Path, org: str, skills: list[AccountSkill], previous_root: Path) -> None:
    previous = config_io.read_json_safe(previous_root / SKILL_MANIFEST)
    owned = previous.get("skills", {})
    if not isinstance(owned, dict):
        owned = {}
    destination = root / "skills"
    destination.mkdir(parents=True, exist_ok=True)
    if root != previous_root and (previous_root / "skills").is_dir():
        shutil.copytree(previous_root / "skills", destination, dirs_exist_ok=True)
    retained = set()
    for skill in skills:
        target = destination / skill.name
        if skill.files is not None:
            if target.exists():
                shutil.rmtree(target)
            write_skill(target, skill.files)
            retained.add(skill.name)
        elif previous.get("org") == org:
            old_name = next((name for name, skill_id in owned.items() if skill_id == skill.id), "")
            if re.fullmatch(r"[a-z0-9-]{1,64}", old_name):
                source = destination / old_name
                if source.is_dir():
                    if source != target:
                        shutil.copytree(source, target, dirs_exist_ok=True)
                    retained.add(skill.name)
    for name in owned:
        if name not in retained and re.fullmatch(r"[a-z0-9-]{1,64}", name):
            path = destination / name
            if path.is_dir():
                shutil.rmtree(path)
    # The first Docs-only plugin had no ownership manifest.
    if not previous and "docs" not in retained and (destination / "docs").is_dir():
        shutil.rmtree(destination / "docs")
    config_io.atomic_write_json(
        root / SKILL_MANIFEST,
        {
            "org": org,
            "skills": {skill.name: skill.id for skill in skills if skill.name in retained},
        },
    )
