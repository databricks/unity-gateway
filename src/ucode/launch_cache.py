"""Opt-in cache for the workspace lookups a launch makes before the agent starts.

A warm `ug claude` launch makes three blocking workspace round trips — the Unity
Gateway probe, model-services discovery, and the Anthropic picker catalog. In far
regions each costs several RTTs. With ``UG_LAUNCH_CACHE=1`` a launch serves those
from the last successful results under ``~/.ucode/launch-cache/`` and spawns a
detached refresher for the next launch (the launch process execs the agent, so an
in-process thread would die with it). `ug configure` never reads this cache.

The entry identity and invalidation rules follow the persisted gateway-check
record (#1389): workspace, profile, auth mode, custom OAuth, and ug version;
configure and login always refresh; a failed refresh invalidates instead of
serving. This cache extends that with the discovery listing and picker catalog,
a credentials-file digest, a 24h max age, and the background refresher.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ucode import config_io, tls
from ucode.databricks import (
    AnthropicModelCatalog,
    GatewayProbe,
    ModelServiceEntry,
    fetch_databricks_token_untimed,
    has_valid_databricks_auth,
    list_anthropic_model_catalog,
    list_model_service_entries,
    prime_gateway_probe_cache,
    prime_model_services_cache,
    probe_unity_gateway_capabilities,
)
from ucode.telemetry import ug_version
from ucode.workspace import pin, workspace_id_args

ENV_VAR = "UG_LAUNCH_CACHE"
CACHE_DIR_NAME = "launch-cache"
ENTRY_VERSION = 1
MAX_AGE_SECONDS = 24 * 60 * 60
# At most one background refresh per key per few minutes, however many launches run.
REFRESH_THROTTLE_SECONDS = 180
# How long a second refresher waits for the first's key lock before giving up on it.
REFRESH_LOCK_TIMEOUT_SECONDS = 30


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


def entry_identity(state: dict) -> dict:
    """The workspace and credentials a cache entry may serve."""
    return {
        "workspace": (state.get("workspace") or "").rstrip("/"),
        "workspace_id": state.get("workspace_id") or "",
        "profile": state.get("profile"),
        "use_pat": bool(state.get("use_pat")),
        "custom_oauth": state.get("custom_oauth"),
        "config_fingerprint": config_fingerprint(),
        "ug_version": ug_version(),
    }


def cache_key(identity: dict) -> str:
    payload = json.dumps([ENTRY_VERSION, identity], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _cache_dir() -> Path:
    directory = config_io.APP_DIR / CACHE_DIR_NAME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _entry_path(key: str) -> Path:
    return _cache_dir() / f"{key}.json"


def _read_entry(key: str, identity: dict | None = None) -> dict | None:
    try:
        entry = json.loads(_entry_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("version") != ENTRY_VERSION:
        return None
    if entry.get("key") != key:
        return None
    if identity is not None and entry.get("identity") != identity:
        return None
    return entry


def load_entry(key: str, identity: dict | None = None) -> dict | None:
    """The cache entry for ``key``, or None when missing, stale, corrupt, or foreign."""
    entry = _read_entry(key, identity)
    if entry is None:
        return None
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)) or time.time() - checked_at > MAX_AGE_SECONDS:
        return None
    return entry


def _part_age_key(part: str) -> str:
    return f"{part}_checked_at"


def _part_fresh(entry: dict, part: str) -> bool:
    """The part's own age: merging one part never refreshes another's, and an entry
    written before per-part ages existed reads as stale."""
    checked_at = entry.get(_part_age_key(part))
    return isinstance(checked_at, (int, float)) and time.time() - checked_at <= MAX_AGE_SECONDS


def merge_entry(key: str, identity: dict, payload: dict) -> None:
    """Merge ``payload`` into the entry; each part carries its own age, and a None
    part drops that part instead of refreshing its age."""
    entry = _read_entry(key) or {}
    now = time.time()
    for part, data in payload.items():
        if data is None:
            entry.pop(part, None)
            entry.pop(_part_age_key(part), None)
        else:
            entry[part] = data
            entry[_part_age_key(part)] = now
    entry["version"] = ENTRY_VERSION
    entry["key"] = key
    entry["identity"] = identity
    entry["checked_at"] = now
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
    if not _part_fresh(entry, "catalog"):
        return None
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
    if not _part_fresh(entry, "probe"):
        return None
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
    if not _part_fresh(entry, "model_services"):
        return None
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


def build_refresher_argv(identity: dict) -> list[str]:
    """Argv for the detached refresher, with the launch's workspace selector on a shared host."""
    encoded = base64.urlsafe_b64encode(json.dumps(identity).encode()).decode()
    argv = [sys.executable, "-P", "-m", "ucode.launch_cache", "--refresh", "--identity", encoded]
    argv += workspace_id_args(identity["workspace"])
    return argv


def spawn_refresher(identity: dict) -> None:
    """Detach a refresher that rewrites the cache for the next launch.

    The launch process execs the agent, so the refresher must outlive it: a new
    session with DEVNULL stdio holds none of the agent's pipes. The identity
    travels as JSON so the refresher re-fetches for exactly this workspace and
    credential set, and re-verifies the credentials file before writing."""
    from ucode.os_compatibility import subprocess_cross_os

    argv = build_refresher_argv(identity)
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
    checked_at = entry.get("checked_at")
    return not isinstance(checked_at, (int, float)) or (
        time.time() - checked_at >= REFRESH_THROTTLE_SECONDS
    )


def _refresh_token(workspace: str, identity: dict) -> str:
    """The token the launch would use, fetched without ever prompting.

    The detached refresher has no terminal, so no re-auth and no custom-OAuth
    login: an auth failure gives up, and the next launch falls back as designed."""
    from ucode.custom_oauth import _custom_cli_profile, custom_oauth_cli_enabled

    custom_oauth = identity.get("custom_oauth")
    if isinstance(custom_oauth, dict) and custom_oauth_cli_enabled(custom_oauth):
        profile = custom_oauth.get("profile") or _custom_cli_profile(
            workspace, custom_oauth.get("client_id") or ""
        )
        if not has_valid_databricks_auth(workspace, profile):
            raise RuntimeError("the custom-OAuth profile has no saved login")
        return fetch_databricks_token_untimed(workspace, profile, allow_reauth=False)
    return fetch_databricks_token_untimed(workspace, identity.get("profile"), allow_reauth=False)


def _invalidate(key: str, seen: dict | None) -> None:
    """Drop the entry, unless another refresher superseded it while this one fetched."""
    current = _read_entry(key)
    if current is None:
        return
    if seen is None or current.get("checked_at") != seen.get("checked_at"):
        return
    _entry_path(key).unlink(missing_ok=True)


def _refresh(identity: dict) -> int:
    from ucode.os_compatibility.file_lock_cross_os import (
        acquire_exclusive_file_lock,
        release_file_lock,
    )

    # A credentials rewrite between the spawn and now must not mint a cache entry
    # for the old identity's fetches.
    if config_fingerprint() != identity.get("config_fingerprint"):
        return 1
    key = cache_key(identity)
    # One refresher per key at a time: a second launch's refresher waits here, then
    # sees the first one's fresh entry, and a failure cannot drop an entry another
    # refresher wrote in between. The lock file stays in place, like the other
    # helpers', so waiters always lock the same inode.
    with (_cache_dir() / f"{key}.lock").open("a+b") as lock_file:
        try:
            acquire_exclusive_file_lock(lock_file, timeout=REFRESH_LOCK_TIMEOUT_SECONDS)
        except TimeoutError:
            # Another refresher holds the key; its write serves the next launch.
            return 0
        try:
            return _refresh_locked(key, identity)
        finally:
            release_file_lock(lock_file)


def _refresh_locked(key: str, identity: dict) -> int:
    from ucode.databricks import clear_model_services_cache

    workspace = identity.get("workspace") or ""
    seen = _read_entry(key)
    if seen is not None:
        checked_at = seen.get("checked_at")
        if isinstance(checked_at, (int, float)) and (
            time.time() - checked_at < REFRESH_THROTTLE_SECONDS
        ):
            return 0
    clear_model_services_cache()
    try:
        token = _refresh_token(workspace, identity)
        probe = probe_unity_gateway_capabilities(workspace, token)
        services, reason = list_model_service_entries(workspace, token)
        if not probe.resource_available or not services or reason is not None:
            # Never leave a failed or empty lookups result behind to serve later.
            _invalidate(key, seen)
            return 1
        catalog = list_anthropic_model_catalog(workspace, token)
        # Only a non-empty catalog is cached, like the launch path: an empty one
        # would suppress the picker for the entry's whole age with no fallback. A
        # failed catalog drops the cached one instead of refreshing its age.
        payload = {
            "probe": probe_payload(probe),
            "model_services": services_payload(services),
            "catalog": None
            if catalog.error_msg or not catalog.model_ids
            else catalog_payload(catalog),
        }
        merge_entry(key, identity, payload)
    except Exception:
        _invalidate(key, seen)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    tls.apply()
    parser = argparse.ArgumentParser(prog="python -m ucode.launch_cache")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--identity", required=True, help="base64url JSON of the entry identity")
    parser.add_argument("--workspace-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.refresh:
        parser.error("--refresh is required")
    try:
        identity = json.loads(base64.urlsafe_b64decode(args.identity.encode()))
    except (ValueError, UnicodeDecodeError):
        return 2
    if not isinstance(identity, dict) or not identity.get("workspace"):
        return 2
    if args.workspace_id:
        pin(identity["workspace"], args.workspace_id)
    return _refresh(identity)


if __name__ == "__main__":
    sys.exit(main())
