"""Build managed Codex catalogs using destination identities and bundled native metadata."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from time import monotonic

from ucode.codex_config import ModelVisibility
from ucode.databricks import (
    custom_model_services,
    get_model_service,
    model_service_destination_models,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.smart_routing.codex_routing import codex_model_id
from ucode.string_utils import model_picker_label
from ucode.ui import print_warning

_TIMEOUT_SECONDS = 30
_SERVICE_LOOKUP_SECONDS = 5
_GPT_FALLBACK_EFFORTS = ("low", "medium", "high")
_PRE_REASONING_GPT_PREFIXES = ("gpt-3", "gpt-4")
_DEFAULT_CONTEXT_WINDOW = 32768

# Known hosted-model capabilities, following universe#2591694. These are not
# universal non-GPT defaults: an arbitrary UC service need not support images,
# a million-token context, or the same reasoning efforts.
_HOSTED_CAPABILITIES = {
    "glm-5-2": (["text"], ["high", "max"]),
    "glm-5-3": (["text"], ["low", "high", "max"]),
    "glm-5-3-flash": (["text", "image"], ["low", "high", "max"]),
    "kimi-k3": (["text", "image"], ["low", "high", "max"]),
    "deepseek-v4-1-flash": (["text", "image"], ["low", "high", "xhigh", "max"]),
    "deepseek-v4-1-flash-1017": (["text", "image"], ["low", "high", "xhigh", "max"]),
}
_HOSTED_DEFAULTS = {
    "input_modalities": ["text"],
    "context_window": _DEFAULT_CONTEXT_WINDOW,
    "max_context_window": _DEFAULT_CONTEXT_WINDOW,
    "default_reasoning_level": "none",
    "supported_reasoning_levels": [{"effort": "none", "description": "Default model behavior"}],
    "default_reasoning_summary": "none",
    "supports_image_detail_original": False,
    "supports_search_tool": False,
    "supports_experimental_context": False,
    "web_search_tool_type": "text",
    "multi_agent_version": None,
    "tool_mode": None,
    "use_responses_lite": False,
    "experimental_supported_tools": [],
    "support_verbosity": False,
    "default_verbosity": None,
    "additional_speed_tiers": [],
    "service_tiers": [],
    "availability_nux": None,
    "upgrade": None,
}


def _model_key(slug: str) -> str:
    # Reuse Codex's known gateway/legacy GPT aliases. Arbitrary UC service
    # names do not establish which underlying model serves them.
    return (
        codex_model_id(slug)
        .removeprefix("system.ai.")
        .removeprefix("databricks-")
        .replace(".", "-")
    )


def _reasoning_levels(efforts) -> list[dict]:
    return [
        {"effort": effort, "description": f"{effort.capitalize()} reasoning"} for effort in efforts
    ]


def _fallback_metadata(
    name: str, model_id: str, bundled_models: list[dict], warn: Callable[[str], None] | None
) -> dict:
    key = _model_key(model_id)
    missing_gpt = key.startswith("gpt-")
    if missing_gpt and warn is not None:
        warn(
            f"Codex is missing metadata for managed GPT model '{model_id}', so UG is "
            "falling back to default metadata. Try updating Codex with `ug codex update`."
        )
    baseline = next((m for m in bundled_models if m.get("tool_mode") is None), None)
    if baseline is None:
        raise RuntimeError(
            f"Codex has no ordinary-tool bundled model to base '{name}' on. Upgrade or reinstall Codex."
        )
    model = copy.deepcopy(baseline)
    model.update(copy.deepcopy(_HOSTED_DEFAULTS))
    # Pre-GPT-5 models reject reasoning efforts, so they keep the none-only default.
    if missing_gpt and not key.startswith(_PRE_REASONING_GPT_PREFIXES):
        model.update(
            default_reasoning_level="medium",
            supported_reasoning_levels=_reasoning_levels(_GPT_FALLBACK_EFFORTS),
        )
    capabilities = _HOSTED_CAPABILITIES.get(key)
    if capabilities:
        modalities, efforts = capabilities
        model.update(
            input_modalities=list(modalities),
            context_window=1048576,
            max_context_window=1048576,
            default_reasoning_level=efforts[-1],
            supported_reasoning_levels=_reasoning_levels(efforts),
        )
    model["description"] = f"{name} served on the Databricks Unity Gateway."
    model["display_name"] = model_picker_label(name)
    return model


def _shared_metadata(name: str, model: dict, destinations: list[dict]) -> dict:
    """Use ordinary tools and capabilities supported by every destination."""
    efforts = [
        level["effort"]
        for level in destinations[0]["supported_reasoning_levels"]
        if all(
            any(other["effort"] == level["effort"] for other in item["supported_reasoning_levels"])
            for item in destinations[1:]
        )
    ]
    if not efforts:
        raise RuntimeError(
            f"Model service '{name}' has no reasoning effort supported by every destination. "
            "Use separate model services for incompatible models."
        )
    defaults = {item.get("default_reasoning_level") for item in destinations}
    default = next(iter(defaults)) if len(defaults) == 1 else None
    if default not in efforts:
        default = "medium" if "medium" in efforts else efforts[0]
    model["supported_reasoning_levels"] = _reasoning_levels(efforts)
    model["default_reasoning_level"] = default
    for field in (
        "input_modalities",
        "additional_speed_tiers",
        "service_tiers",
        "experimental_supported_tools",
    ):
        model[field] = [
            value
            for value in destinations[0].get(field, [])
            if all(value in item.get(field, []) for item in destinations[1:])
        ]
    if not model["input_modalities"]:
        raise RuntimeError(
            f"Model service '{name}' has no input modality supported by every destination. "
            "Use separate model services for incompatible models."
        )
    for field in ("context_window", "max_context_window"):
        model[field] = min(
            item.get(field) or item.get("context_window") or _DEFAULT_CONTEXT_WINDOW
            for item in destinations
        )
    model["effective_context_window_percent"] = min(
        item.get("effective_context_window_percent") or 95 for item in destinations
    )
    for field in (
        "supports_image_detail_original",
        "supports_search_tool",
        "supports_experimental_context",
        "support_verbosity",
        "use_responses_lite",
        "supports_reasoning_effort_updates",
    ):
        if field in model:
            model[field] = all(item.get(field, False) for item in destinations)
    if "image" not in model["input_modalities"]:
        model["supports_image_detail_original"] = False
    for field in ("node_repl_disabled", "node_repl_auto_review_required"):
        if field in model:
            model[field] = any(item.get(field, False) for item in destinations)
    model["default_verbosity"] = "medium" if model.get("support_verbosity") else None
    if "default_service_tier" in model:
        model["default_service_tier"] = None
    return model


def build_codex_catalog(
    bundled_models: list[dict],
    names: list[str],
    *,
    warn: Callable[[str], None] | None = None,
    model_ids: dict[str, list[str]] | None = None,
) -> dict:
    """Copy native presets or derive generic compatibility defaults, in admin order.

    Preserve all native metadata (including unknown future fields), except picker
    slug/visibility/order and custom-service labels. Custom services and models without native
    metadata use their unqualified name, or the full id when labels collide. Only requested slugs are visible.
    Hidden aliases for selected GPTs support the smart router's native-ID translation.
    """
    by_slug = {model["slug"]: model for model in bundled_models}
    by_key = {_model_key(slug): model for slug, model in by_slug.items()}
    models = []
    for name in dict.fromkeys(names):
        targets = list(
            {
                _model_key(target): target for target in ((model_ids or {}).get(name) or [name])
            }.values()
        )
        destinations = []
        for model_id in targets:
            native = by_slug.get(model_id) or by_key.get(_model_key(model_id))
            destinations.append(
                copy.deepcopy(native)
                if native is not None
                else _fallback_metadata(name, model_id, bundled_models, warn)
            )
        model = destinations[0]
        if len(destinations) > 1:
            model = _shared_metadata(
                name, _fallback_metadata(name, name, bundled_models, None), destinations
            )
        if name in (model_ids or {}):
            model["display_name"] = model_picker_label(name)
        model.update(
            slug=name,
            visibility=ModelVisibility.LIST,
            supported_in_api=True,
            priority=len(models),
        )
        models.append(model)
    labels = [model["display_name"] for model in models]
    for model in models:
        # Readable labels may collide across catalogs/schemas; the full id is unique.
        if labels.count(model["display_name"]) > 1:
            model["display_name"] = model["slug"]
    requested = set(names)
    aliases = {}
    for model in models:
        native_slug = codex_model_id(model["slug"])
        if native_slug != model["slug"] and native_slug not in requested:
            alias = copy.deepcopy(model)
            alias.update(slug=native_slug, visibility=ModelVisibility.HIDE)
            aliases.setdefault(native_slug, alias)
    models.extend(aliases.values())
    return {"models": models}


def _run_catalog_command(
    binary: str, args: list[str], home: str
) -> subprocess.CompletedProcess[str]:
    return subprocess_cross_os.run(
        [binary, *args],
        env={**os.environ, "CODEX_HOME": home},
        cwd=home,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=True,
    )


def _validate_codex_catalog_in_home(binary: str, catalog: dict, home: str) -> None:
    candidate = Path(home) / "catalog.json"
    candidate.write_text(json.dumps(catalog), encoding="utf-8")
    _run_catalog_command(
        binary,
        [
            "-c",
            f"model_catalog_json={json.dumps(str(candidate))}",
            "debug",
            "models",
        ],
        home,
    )


def validate_codex_catalog(binary: str, catalog: dict) -> None:
    """Validate a candidate catalog with the selected Codex binary in isolation."""
    try:
        with tempfile.TemporaryDirectory(prefix="ug-codex-catalog-") as home:
            _validate_codex_catalog_in_home(binary, catalog, home)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        raise RuntimeError(
            "Could not load the Codex model catalog with the selected Codex binary. "
            "Update Codex and retry `ug codex`."
        ) from None


def _model_service_model_ids(
    names: list[str],
    workspace: str | None,
    get_token: Callable[[], str] | None,
) -> dict[str, list[str]]:
    services = list(dict.fromkeys(custom_model_services(names)))
    if not services or not workspace or get_token is None:
        return {}
    try:
        token = get_token()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        print_warning(
            "Could not authenticate to resolve Codex model-service metadata. "
            "Using generic compatibility metadata; check your workspace login."
        )
        return {}
    model_ids = {}
    deadline = monotonic() + _SERVICE_LOOKUP_SECONDS
    for name in services:
        remaining = deadline - monotonic()
        service, reason = (
            get_model_service(name.removesuffix("[1m]"), workspace, token, timeout=remaining)
            if remaining > 0
            else (None, "metadata lookup time budget exhausted")
        )
        targets = model_service_destination_models(service) if service is not None else None
        if targets:
            model_ids[name] = targets
        else:
            detail = reason or "destinations do not expose backing model identities"
            print_warning(
                f"Could not resolve Codex metadata for model service '{name}': {detail}. "
                "Using generic compatibility metadata; check the service's routing configuration."
            )
    return model_ids


def prepare_codex_catalog(
    binary: str,
    names: list[str],
    *,
    workspace: str | None = None,
    get_token: Callable[[], str] | None = None,
    include_bundled: bool = False,
) -> dict:
    """Resolve custom services' destinations, then extract and validate native metadata.

    --bundled bypasses discovery. Validation reads an explicit local catalog.
    Both commands run outside user/project config in a disposable CODEX_HOME.
    Failure blocks configuration rather than falling back to remote Codex discovery.
    Routing without a scoped catalog retains the native menu and adds missing router models.
    """
    model_ids = _model_service_model_ids(names, workspace, get_token)
    try:
        with tempfile.TemporaryDirectory(prefix="ug-codex-catalog-") as home:
            result = _run_catalog_command(binary, ["debug", "models", "--bundled"], home)
            payload = json.loads(result.stdout)
            bundled = payload.get("models") if isinstance(payload, dict) else None
            if (
                not isinstance(bundled, list)
                or not bundled
                or not all(
                    isinstance(model, dict) and isinstance(model.get("slug"), str) and model["slug"]
                    for model in bundled
                )
            ):
                raise ValueError("invalid bundled model catalog")
            if include_bundled:
                native_keys = {_model_key(model["slug"]) for model in bundled}
                names = [name for name in names if _model_key(name) not in native_keys]
            fallback_warnings: list[str] = []
            catalog = build_codex_catalog(
                bundled, names, warn=fallback_warnings.append, model_ids=model_ids
            )
            if include_bundled:
                priority = max(model.get("priority", 0) for model in bundled) + 1
                for offset, model in enumerate(catalog["models"]):
                    model["priority"] = priority + offset
                catalog["models"] = [*bundled, *catalog["models"]]
            _validate_codex_catalog_in_home(binary, catalog, home)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        raise RuntimeError(
            "Could not build the Codex model catalog locally. "
            "Upgrade the active Codex installation and verify "
            "`codex debug models --bundled` works, then retry. "
            "Gateway discovery was not used."
        ) from None
    for message in fallback_warnings:
        print_warning(message)
    return catalog
