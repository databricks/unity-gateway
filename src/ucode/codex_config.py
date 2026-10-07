"""Shared Codex configuration helpers."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from enum import StrEnum
from pathlib import Path

import tomlkit
from tomlkit.items import Item

from ucode.config_io import read_json_safe, read_toml_safe
from ucode.managed_files import OS, current_os
from ucode.os_compatibility import subprocess_cross_os
from ucode.ui import print_warning

CODEX_PROFILE_NAME = "ucode"
DEFAULT_CODEX_CONFIG_PATH = Path.home() / ".codex" / f"{CODEX_PROFILE_NAME}.config.toml"
CONFIG_READ_TIMEOUT_SECONDS = 15


def codex_working_directory(tool_args: list[str]) -> Path:
    """Resolve the launch directory before looking up project configuration."""
    directory = Path.cwd()
    args = iter(tool_args)
    for arg in args:
        if arg == "--":
            break
        if arg in {"--cd", "-C"}:
            value = next(args, None)
            if value is not None:
                directory = Path(value).expanduser()
        elif arg.startswith("--cd="):
            directory = Path(arg.partition("=")[2]).expanduser()
        elif arg.startswith("-C"):
            directory = Path(arg[2:].removeprefix("=")).expanduser()
    return directory.resolve()


def codex_cli_config_args(tool_args: list[str]) -> list[str]:
    """Keep caller configuration overrides, including project trust, in the native lookup."""
    config_args = []
    args = iter(tool_args)
    options = {"-c", "--config", "--enable", "--disable"}
    for arg in args:
        if arg == "--":
            break
        if arg in options:
            value = next(args, None)
            if value is not None:
                config_args.extend([arg, value])
        elif arg.partition("=")[0] in options or arg.startswith("-c"):
            config_args.append(arg)
    return config_args


def read_effective_codex_config(binary: str, *, cwd: Path, config_args: list[str]) -> dict:
    """Let Codex apply its configuration precedence and project trust rules."""
    error = (
        "Could not read Codex configuration for smart routing. Check your Codex configuration "
        "or launch with --disable-smart-routing."
    )
    try:
        process = subprocess_cross_os.popen(
            [binary, "app-server", *config_args, "--listen", "stdio://"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(error) from exc
    stdin, stdout = process.stdin, process.stdout
    assert stdin is not None and stdout is not None
    messages: queue.Queue = queue.Queue()

    def read_messages() -> None:
        try:
            for line in stdout:
                messages.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            messages.put(None)

    reader = threading.Thread(target=read_messages, daemon=True)
    reader.start()
    deadline = time.monotonic() + CONFIG_READ_TIMEOUT_SECONDS

    def request(request_id: int, method: str, params: dict) -> dict:
        stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        stdin.flush()
        while time.monotonic() < deadline:
            message = messages.get(timeout=max(0, deadline - time.monotonic()))
            if not isinstance(message, dict):
                raise RuntimeError(error)
            if message.get("id") == request_id:
                result = message.get("result")
                if not isinstance(result, dict) or "error" in message:
                    raise RuntimeError(error)
                return result
        raise RuntimeError(error)

    try:
        request(1, "initialize", {"clientInfo": {"name": "unity-gateway", "version": "1"}})
        stdin.write('{"method":"initialized","params":{}}\n')
        stdin.flush()
        result = request(2, "config/read", {"cwd": str(cwd), "includeLayers": False})
        config = result.get("config")
        if not isinstance(config, dict):
            raise RuntimeError(error)
        return config
    except (OSError, queue.Empty) as exc:
        raise RuntimeError(error) from exc
    finally:
        with suppress(OSError):
            stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=5)
        stdout.close()


class ModelVisibility(StrEnum):
    """A model's visibility in Codex's picker/APIs (mirrors Codex's ModelVisibility)."""

    LIST = "list"
    HIDE = "hide"
    NONE = "none"


def codex_managed_config_path() -> Path | None:
    if current_os() in (OS.LINUX, OS.MACOS):
        return Path("/etc/codex/managed_config.toml")
    return None


def codex_config_precedence_paths(
    managed_path: Path | None,
    profile_path: Path,
) -> tuple[Path, ...]:
    """Return Codex config paths in managed, profile, then user precedence."""
    config_home = os.environ.get("CODEX_HOME")
    if config_home:
        profile_path = Path(config_home).expanduser() / f"{CODEX_PROFILE_NAME}.config.toml"

    # Highest precedence: machine-managed settings, normally /etc/codex/managed_config.toml.
    managed_config = managed_path
    # Middle precedence: Unity Gateway's ucode.config.toml layer passed to Codex via the CLI.
    cli_config = profile_path
    # Lowest precedence: $CODEX_HOME/config.toml, or ~/.codex/config.toml by default.
    default_config = profile_path.parent / "config.toml"
    return tuple(path for path in (managed_config, cli_config, default_config) if path is not None)


def custom_catalog_path() -> Path | None:
    """Resolve the configured model catalog using Codex config precedence."""
    try:
        paths = codex_config_precedence_paths(
            codex_managed_config_path(),
            DEFAULT_CODEX_CONFIG_PATH,
        )
    except OSError:
        return None
    for path in paths:
        if not path.is_file():
            continue
        settings = read_toml_safe(path)
        catalog_ref = settings.get("model_catalog_json")
        if not isinstance(catalog_ref, str) or not catalog_ref.strip():
            continue
        return Path(catalog_ref).expanduser()
    return None


def custom_catalog_models() -> list[str] | None:
    """Read model slugs from the configured model_catalog_json, if present."""
    catalog_path = custom_catalog_path()
    if catalog_path is None:
        return None
    slugs = catalog_slugs(read_json_safe(catalog_path), required_visibility=ModelVisibility.LIST)
    if slugs:
        return slugs
    print_warning(
        f"Codex smart routing could not read models from the custom catalog {catalog_path}; "
        "falling back to the cached model services."
    )
    return None


def catalog_slugs(
    catalog: Mapping, *, required_visibility: ModelVisibility | None = None
) -> list[str]:
    """Extract deduplicated model slugs from a Codex custom catalog mapping."""
    models = catalog.get("models")
    if not isinstance(models, list):
        return []
    slugs: list[str] = []
    seen: set[str] = set()
    for row in models:
        if not isinstance(row, dict) or not isinstance(row.get("slug"), str):
            continue
        if required_visibility is not None and row.get("visibility") != required_visibility:
            continue
        slug = row["slug"].strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def _toml_item(value: object) -> Item:
    if isinstance(value, Mapping):
        inline = tomlkit.inline_table()
        for key, child in value.items():
            # Native config/read includes null optional fields; TOML represents them by omission.
            if child is None:
                continue
            inline[str(key)] = _toml_item(child)
        return inline
    if isinstance(value, list):
        array = tomlkit.array()
        for child in value:
            array.append(_toml_item(child))
        return array
    if isinstance(value, Item):
        return value
    return tomlkit.item(value)


def _toml_value(value: object) -> str:
    return _toml_item(value).as_string()


def codex_config_args(config: dict) -> list[str]:
    """Render a Codex config layer as repeatable ``--config`` overrides."""
    args: list[str] = []
    for key, value in config.items():
        # These maps contain named entries. Override each entry individually so
        # the rest of the user's base map remains intact.
        if key in {"hooks", "model_providers"} and isinstance(value, dict):
            for entry_name, entry_config in value.items():
                args.extend(
                    [
                        "--config",
                        f"{key}.{entry_name}={_toml_value(entry_config)}",
                    ]
                )
        else:
            args.extend(["--config", f"{key}={_toml_value(value)}"])
    return args
