"""Resolve native settings files before building a managed installation.

Published configurations reference ordinary UC Volume files read as the configuring principal.
Draft previews read local files relative to the draft, without authenticating to a workspace.
"""

from __future__ import annotations

import copy
import json
import tomllib
from contextlib import closing
from pathlib import Path

from ucode.workspace import workspace_headers

MAX_NATIVE_CONFIG_FILE_BYTES = 1024 * 1024
_NATIVE_FILE_ROLES = {
    "claude": ("claude_code", ("managed_settings",)),
    "codex": ("codex", ("config", "requirements")),
}


def validate_volume_file_path(path: object) -> str:
    """Return a canonical, complete UC Volume file path or raise ValueError."""
    if (
        not isinstance(path, str)
        or len(path) > 4096
        or not path.startswith("/Volumes/")
        or len(path.split("/")) < 6
        or any(part in ("", ".", "..") for part in path.split("/")[1:])
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ValueError(
            "expected /Volumes/<catalog>/<schema>/<volume>/<file> without empty, . or .. segments"
        )
    return path


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not a JSON value")


def _parse_native_file(data: bytes, variant: str) -> dict:
    if len(data) > MAX_NATIVE_CONFIG_FILE_BYTES:
        raise ValueError(f"file exceeds {MAX_NATIVE_CONFIG_FILE_BYTES} bytes")
    text = data.decode("utf-8")
    parsed = (
        json.loads(text, parse_constant=_reject_json_constant)
        if variant == "claude_code"
        else tomllib.loads(text)
    )
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def resolve_native_config_files(
    manifest: dict,
    workspace: str,
    *,
    agents: list[str],
    token: str | None = None,
    source: str | None = None,
) -> dict:
    """Materialize selected agents' files into a copy of a normalized configuration.

    Each role accepts either inline settings or a file. All reads and parsing finish before the
    caller writes settings. No file cache is used, so explicit configure sees same-path edits.
    ValueError reports invalid input; OSError reports a failed remote download.
    """
    result = copy.deepcopy(manifest)
    client = None
    for tool in agents:
        entry = (result.get("enabled_agents") or {}).get(tool)
        if not isinstance(entry, dict) or tool not in _NATIVE_FILE_ROLES:
            continue
        variant, roles = _NATIVE_FILE_ROLES[tool]
        native = entry.get(variant)
        if not isinstance(native, dict):
            continue
        for role in roles:
            reference = f"{role}_file"
            if reference not in native:
                continue
            field = f"agent_native_config.{variant}.{reference}"
            path = native[reference]
            if role in native:
                raise ValueError(f"{field}: choose inline {role} or {reference}, not both")
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                raise ValueError(f"{field}: expected a non-empty file path")
            try:
                if source is not None:
                    local = Path(path).expanduser()
                    if not local.is_absolute():
                        local = Path(source).expanduser().absolute().parent / local
                    with local.open("rb") as stream:
                        data = stream.read(MAX_NATIVE_CONFIG_FILE_BYTES + 1)
                else:
                    validate_volume_file_path(path)
                    if client is None:
                        if token is None:
                            raise OSError("workspace credentials are required to read native files")
                        from databricks.sdk import WorkspaceClient

                        selection = workspace_headers(workspace)
                        client = WorkspaceClient(
                            host=workspace,
                            token=token,
                            auth_type="pat",
                            custom_headers=selection,
                        )
                        # FilesAPI supplies its own workspace header, which outranks custom headers.
                        client.config.workspace_id = selection.get("X-Databricks-Workspace-Id", "")
                    from databricks.sdk.errors import DatabricksError

                    try:
                        response = client.files.download(path)
                        if response.contents is None:
                            raise OSError("Files API returned no contents")
                        with closing(response.contents) as stream:
                            data = stream.read(MAX_NATIVE_CONFIG_FILE_BYTES + 1)
                    except DatabricksError as exc:
                        raise OSError(str(exc)) from exc
                    except OSError as exc:
                        from ucode.databricks import _network_error_reason

                        raise OSError(_network_error_reason(exc)) from exc
                native[role] = _parse_native_file(data, variant)
            except (ValueError, UnicodeError, OSError) as exc:
                message = f"{field} ({path}): {exc}"
                if isinstance(exc, OSError) and source is None:
                    raise OSError(message) from exc
                raise ValueError(message) from exc
            del native[reference]
    return result
