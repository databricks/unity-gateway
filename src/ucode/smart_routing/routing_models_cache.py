"""On-disk cache of the models smart routing may choose from, per workspace and provider.

Launches fetch a catalog and record it here, so a launch whose fetch fails can fall back to the
last good list. Kept in its own file, ``~/.ucode/routing-models.json``, so a ``state.json``
version change or ``ug revert`` leaves it alone. It holds model metadata only, never tokens or
other credentials.

Layout (version 1)::

    {"version": 1,
     "workspaces": {"<workspace key>": {"default": [<model>, ...],
                                        "providers": {"<catalog.schema.name>": [<model>, ...]}}}}

``default`` is the list for no provider service (the ``system.ai`` catalog). Keeping it apart from
``providers`` means no provider name can collide with it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ucode import config_io
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)

if TYPE_CHECKING:
    from ucode.databricks import AnthropicModelCatalog

ROUTING_MODELS_CACHE_VERSION = 1
_DEFAULT_KEY = "default"
_PROVIDERS_KEY = "providers"
# Concurrent launches share the file; a stalled writer must not hold a launch for long.
_LOCK_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RoutingModel:
    """One routable model, with ``id`` exactly as the gateway advertised it."""

    id: str
    display_name: str | None = None
    max_input_tokens: int | None = None


def routing_models_cache_path() -> Path:
    # Resolved per call so a redirected APP_DIR (tests, alternate homes) is honored.
    return config_io.APP_DIR / "routing-models.json"


def routing_models_from_catalog(catalog: AnthropicModelCatalog) -> list[RoutingModel]:
    """The catalog's models in advertised order; a reported limit of 0 means unknown."""
    models = []
    for model_id in catalog.model_ids:
        limits = catalog.model_id_to_limits.get(model_id)
        max_input = limits[0] if limits and limits[0] > 0 else None
        models.append(
            RoutingModel(
                id=model_id,
                display_name=catalog.model_id_to_display_name.get(model_id) or None,
                max_input_tokens=max_input,
            )
        )
    return models


def write_routing_models(
    workspace: str,
    provider_service: str | None,
    models: list[RoutingModel],
) -> bool:
    """Replace the cached list for ``(workspace, provider_service)``; other entries are kept.

    ``workspace`` is the state key (``workspace_key`` / ``state_workspace_key``) so hosts serving
    several workspaces stay separate. Best-effort: returns False instead of raising when the cache
    cannot be written, and skips the write under dry-run.
    """
    if not workspace or config_io.is_dry_run():
        return False
    path = routing_models_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(path.with_name(f"{path.name}.lock"), "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        acquire_exclusive_file_lock(lock, timeout=_LOCK_TIMEOUT_SECONDS)
    except (OSError, TimeoutError):
        lock.close()
        return False
    try:
        # Load inside the lock so a concurrent writer's entry is merged, not overwritten.
        return _write_locked(path, workspace, provider_service, models)
    finally:
        release_file_lock(lock)
        lock.close()


def _write_locked(
    path: Path, workspace: str, provider_service: str | None, models: list[RoutingModel]
) -> bool:
    data = _load(path)
    workspaces = data.get("workspaces")
    workspaces = dict(workspaces) if isinstance(workspaces, dict) else {}
    block = workspaces.get(workspace)
    block = dict(block) if isinstance(block, dict) else {}
    rows = routing_models_to_json(models)
    if provider_service:
        providers = block.get(_PROVIDERS_KEY)
        providers = dict(providers) if isinstance(providers, dict) else {}
        providers[provider_service] = rows
        block[_PROVIDERS_KEY] = providers
    else:
        block[_DEFAULT_KEY] = rows
    workspaces[workspace] = block
    try:
        config_io.atomic_write_json(
            path, {"version": ROUTING_MODELS_CACHE_VERSION, "workspaces": workspaces}
        )
    except RuntimeError:
        return False
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass
    return True


def read_routing_models(workspace: str, provider_service: str | None) -> list[RoutingModel] | None:
    """The cached list for ``(workspace, provider_service)``, or None when there is none.

    Never raises: routing hooks call this, so a missing, unreadable, corrupt, or other-version
    file reads as no cache. Malformed rows are skipped.
    """
    if not workspace:
        return None
    workspaces = _load(routing_models_cache_path()).get("workspaces")
    block = workspaces.get(workspace) if isinstance(workspaces, dict) else None
    if not isinstance(block, dict):
        return None
    if provider_service:
        providers = block.get(_PROVIDERS_KEY)
        rows = providers.get(provider_service) if isinstance(providers, dict) else None
    else:
        rows = block.get(_DEFAULT_KEY)
    if not isinstance(rows, list):
        return None
    return routing_models_from_json(rows)


def routing_models_to_json(models: list[RoutingModel]) -> list[dict]:
    """JSON rows for ``models``, as the cache file stores them."""
    return [_model_to_json(model) for model in models]


def routing_models_from_json(rows: list) -> list[RoutingModel]:
    """The well-formed models in JSON ``rows``; malformed rows are skipped."""
    return [model for row in rows if (model := _model_from_json(row)) is not None]


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != ROUTING_MODELS_CACHE_VERSION:
        return {}
    return data


def _model_to_json(model: RoutingModel) -> dict:
    row: dict[str, object] = {"id": model.id}
    if model.display_name is not None:
        row["display_name"] = model.display_name
    if model.max_input_tokens is not None:
        row["max_input_tokens"] = model.max_input_tokens
    return row


def _model_from_json(row: object) -> RoutingModel | None:
    if not isinstance(row, dict):
        return None
    model_id = row.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    display_name = row.get("display_name")
    max_input = row.get("max_input_tokens")
    return RoutingModel(
        id=model_id,
        display_name=display_name if isinstance(display_name, str) and display_name else None,
        max_input_tokens=(
            max_input
            if isinstance(max_input, int) and not isinstance(max_input, bool) and max_input > 0
            else None
        ),
    )
