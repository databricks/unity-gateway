"""Opt-in cache for the workspace lookups a launch makes before the agent starts.

A warm `ug claude` launch makes three blocking workspace round trips — the Unity
Gateway probe, model-services discovery, and the Anthropic picker catalog. In far
regions each costs several RTTs. With ``UG_LAUNCH_CACHE=1`` a launch serves those
from the last successful results under ``~/.ucode/launch-cache/`` and spawns a
detached refresher for the next launch (the launch process execs the agent, so an
in-process thread would die with it). `ug configure` never reads this cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ucode import config_io
from ucode.databricks import (
    AnthropicModelCatalog,
    GatewayProbe,
    ModelServiceEntry,
    get_databricks_token,
    list_anthropic_model_catalog,
    list_model_service_entries,
    prime_gateway_probe_cache,
    prime_model_services_cache,
    probe_unity_gateway_capabilities,
)
from ucode.telemetry import ug_version

ENV_VAR = "UG_LAUNCH_CACHE"
CACHE_DIR_NAME = "launch-cache"
ENTRY_VERSION = 1
MAX_AGE_SECONDS = 24 * 60 * 60
# At most one background refresh per key per few minutes, however many launches run.
REFRESH_THROTTLE_SECONDS = 180


def launch_cache_enabled() -> bool:
    return os.environ.get(ENV_VAR) == "1"


def config_fingerprint() -> dict:
    """Identity of the credentials file, so a re-login or profile rewrite misses the cache.

    The file is hashed locally: a same-size in-place edit can land inside the
    filesystem's mtime granularity, and only the digest is ever stored."""
    path = Path(os.environ.get("DATABRICKS_CONFIG_FILE") or "~/.databrickscfg").expanduser()
    try:
        data = path.read_bytes()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def cache_key(workspace: str, profile: str | None, workspace_id: str | None) -> str:
    payload = json.dumps(
        [
            ENTRY_VERSION,
            workspace.rstrip("/"),
            profile or "",
            workspace_id or "",
            config_fingerprint(),
            ug_version(),
        ],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def entry_key_for_state(state: dict) -> str | None:
    workspace = state.get("workspace")
    if not workspace:
        return None
    return cache_key(workspace, state.get("profile"), state.get("workspace_id"))


def _cache_dir() -> Path:
    directory = config_io.APP_DIR / CACHE_DIR_NAME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _entry_path(key: str) -> Path:
    return _cache_dir() / f"{key}.json"


def _read_entry(key: str) -> dict | None:
    try:
        entry = json.loads(_entry_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("version") != ENTRY_VERSION:
        return None
    if entry.get("key") != key:
        return None
    return entry


def load_entry(key: str) -> dict | None:
    """The cache entry for ``key``, or None when missing, stale, or corrupt."""
    entry = _read_entry(key)
    if entry is None:
        return None
    saved_at = entry.get("saved_at")
    if not isinstance(saved_at, (int, float)) or time.time() - saved_at > MAX_AGE_SECONDS:
        return None
    return entry


def merge_entry(key: str, payload: dict) -> None:
    """Merge ``payload`` into the entry, refreshing its age; parts stay independent."""
    entry = _read_entry(key) or {}
    entry.update(payload)
    entry["version"] = ENTRY_VERSION
    entry["key"] = key
    entry["saved_at"] = time.time()
    try:
        # mkstemp-created, so the replaced file is 0600 like the rest of ~/.ucode.
        config_io.atomic_write_json(_entry_path(key), entry)
    except (OSError, RuntimeError):
        pass


def clear_cache() -> None:
    """Drop every entry (configure and login invalidate; keys isolate the rest)."""
    directory = config_io.APP_DIR / CACHE_DIR_NAME
    try:
        for path in directory.glob("*.json"):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def entry_catalog(entry: dict) -> AnthropicModelCatalog | None:
    catalog = entry.get("catalog")
    if not isinstance(catalog, dict):
        return None
    limits = {}
    for model_id, pair in (catalog.get("limits") or {}).items():
        if isinstance(pair, list) and len(pair) == 2:
            limits[model_id] = (pair[0], pair[1])
    return AnthropicModelCatalog(
        model_ids=list(catalog.get("model_ids") or []),
        model_id_to_display_name=dict(catalog.get("display_names") or {}),
        model_id_to_description=dict(catalog.get("descriptions") or {}),
        model_id_to_limits=limits,
    )


def entry_probe(entry: dict) -> GatewayProbe | None:
    probe = entry.get("probe")
    # Only a verified-available gateway is cached; anything else blocks as today.
    if not isinstance(probe, dict) or not probe.get("resource_available"):
        return None
    return GatewayProbe(
        reachable=bool(probe.get("reachable")),
        detail=str(probe.get("detail") or ""),
        resource_available=True,
        conclusive=bool(probe.get("conclusive")),
    )


def entry_model_services(entry: dict) -> list[ModelServiceEntry] | None:
    entries = entry.get("model_services")
    if not isinstance(entries, list) or not entries:
        return None
    services = []
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            services.append(ModelServiceEntry(item["id"], tuple(item.get("api_types") or ())))
    return services or None


def prime_from_entry(workspace: str, token: str, entry: dict) -> None:
    """Seed the per-process caches so the launch's lookups answer without network."""
    services = entry_model_services(entry)
    if services is not None:
        prime_model_services_cache(workspace, services)
    probe = entry_probe(entry)
    if probe is not None:
        prime_gateway_probe_cache(workspace, token, probe)


def catalog_payload(catalog: AnthropicModelCatalog) -> dict:
    return {
        "model_ids": catalog.model_ids,
        "display_names": catalog.model_id_to_display_name,
        "descriptions": catalog.model_id_to_description,
        "limits": {model_id: list(pair) for model_id, pair in catalog.model_id_to_limits.items()},
    }


def probe_payload(probe: GatewayProbe) -> dict:
    return {
        "reachable": probe.reachable,
        "detail": probe.detail,
        "resource_available": True,
        "conclusive": probe.conclusive,
    }


def services_payload(services: list[ModelServiceEntry]) -> list[dict]:
    return [{"id": service.id, "api_types": list(service.api_types)} for service in services]


def spawn_refresher(workspace: str, profile: str | None, workspace_id: str | None) -> None:
    """Detach a refresher that rewrites the cache for the next launch.

    The launch process execs the agent, so the refresher must outlive it: a new
    session with DEVNULL stdio holds none of the agent's pipes."""
    from ucode.os_compatibility import subprocess_cross_os

    argv = [
        sys.executable,
        "-P",
        "-m",
        "ucode.launch_cache",
        "--refresh",
        "--workspace",
        workspace,
        "--profile",
        profile or "",
        "--workspace-id",
        workspace_id or "",
    ]
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    try:
        subprocess_cross_os.popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **options,
        )
    except OSError:
        pass


def refresh_due(key: str) -> bool:
    entry = _read_entry(key)
    if entry is None:
        return False
    saved_at = entry.get("saved_at")
    return not isinstance(saved_at, (int, float)) or (
        time.time() - saved_at >= REFRESH_THROTTLE_SECONDS
    )


def _refresh(workspace: str, profile: str, workspace_id: str) -> int:
    from ucode.databricks import clear_model_services_cache

    key = cache_key(workspace, profile or None, workspace_id or None)
    entry = _read_entry(key)
    if entry is not None:
        saved_at = entry.get("saved_at")
        if isinstance(saved_at, (int, float)) and (
            time.time() - saved_at < REFRESH_THROTTLE_SECONDS
        ):
            return 0
    clear_model_services_cache()
    try:
        token = get_databricks_token(workspace, profile or None)
        probe = probe_unity_gateway_capabilities(workspace, token)
        services, reason = list_model_service_entries(workspace, token)
        if not probe.resource_available or not services or reason is not None:
            # Never leave a failed or empty lookups result behind to serve later.
            _entry_path(key).unlink(missing_ok=True)
            return 1
        catalog = list_anthropic_model_catalog(workspace, token)
        payload = {
            "probe": probe_payload(probe),
            "model_services": services_payload(services),
        }
        if not catalog.error_msg:
            payload["catalog"] = catalog_payload(catalog)
        merge_entry(key, payload)
    except Exception:
        _entry_path(key).unlink(missing_ok=True)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ucode.launch_cache")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--profile", default="")
    parser.add_argument("--workspace-id", default="")
    args = parser.parse_args(argv)
    if not args.refresh:
        parser.error("--refresh is required")
    return _refresh(args.workspace, args.profile, args.workspace_id)


if __name__ == "__main__":
    sys.exit(main())
