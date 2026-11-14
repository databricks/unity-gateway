"""Fixed native model identities behind Claude's single Auto picker row."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from ucode.databricks import AnthropicModelCatalog

AUTO_MODEL = "ug-auto"  # Legacy identity, accepted during migration only.
AUTO_PREFIX = "ug-auto-"
AUTO_DESCRIPTION = "Delivers frontier-model quality at lower cost through Smart Routing."
AUTO_CUSTOM_MODEL_ENV_KEYS = (
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
)


def is_auto_model(model: str) -> bool:
    return model == AUTO_MODEL or model.startswith(AUTO_PREFIX)


def model_behavior(row: dict) -> str | None:
    """Use explicit capability metadata or the exact Claude ID, never a label."""
    explicit = row.get("behavesAs")
    if isinstance(explicit, str) and explicit:
        return explicit
    model = row.get("model", "").removeprefix("system.ai.")
    if re.fullmatch(r"claude-(?:opus|sonnet|haiku|fable)-\d+(?:-\d+)*(?:\[1m\])?", model):
        return model
    return None


def auto_variant(row: dict) -> str:
    # Keep the native capability-bearing ID intact, including its context suffix.
    # The digest separates deployments that deliberately share a capability profile.
    digest = hashlib.sha256(row["model"].encode()).hexdigest()[:12]
    return AUTO_PREFIX + digest + "-" + (model_behavior(row) or row["model"])


def model_available(model: str, settings: dict) -> bool:
    """Conservatively keep exact configured targets, including context suffixes."""

    def matches(entry: object) -> bool:
        return isinstance(entry, str) and entry in (model, model.split("[", 1)[0])

    allowed = settings.get("availableModels")
    denied = settings.get("deniedModels")
    return (not allowed or any(matches(entry) for entry in allowed)) and not (
        isinstance(denied, list) and any(matches(entry) for entry in denied)
    )


def add_picker_entry(settings: dict) -> dict:
    """Install immutable aliases and one native custom row at explicit configure.

    Aliases are not modelPicker rows: identical labels would create duplicate rows.
    The mod changes only the process-local custom option, never shared profiles.
    """
    picker = settings.get("modelPicker")
    if not isinstance(picker, dict) or not isinstance(picker.get("options", []), list):
        return settings
    if not isinstance(settings.get("modelOverrides", {}), dict):
        return settings
    env = settings.get("env", {})
    if not isinstance(env, dict):
        return settings
    custom = env.get("ANTHROPIC_CUSTOM_MODEL_OPTION")
    if custom and (not isinstance(custom, str) or not is_auto_model(custom)):
        return settings
    rows = [
        row
        for row in picker.get("options", [])
        if isinstance(row, dict)
        and isinstance(row.get("model"), str)
        and not is_auto_model(row["model"])
    ]
    available = [
        row
        for row in rows
        if model_available(row["model"], settings)
        and model_available(auto_variant(row), {"deniedModels": settings.get("deniedModels")})
    ]
    result = deepcopy(settings)
    # Remove obsolete UG aliases, including targets withdrawn by the administrator.
    overrides = result.get("modelOverrides", {})
    result["modelOverrides"] = overrides
    for model in list(overrides):
        if is_auto_model(model):
            del overrides[model]
    allowed = result.get("availableModels")
    if isinstance(allowed, list):
        remaining = [
            model for model in allowed if not isinstance(model, str) or not is_auto_model(model)
        ]
        # An empty native allowlist means unrestricted, not deny-all. Keep the
        # withdrawn IDs (without mappings) if they are the only permitted entries.
        if remaining:
            allowed[:] = remaining
    env = result.setdefault("env", {})
    if isinstance(custom, str) and is_auto_model(custom):
        for key in AUTO_CUSTOM_MODEL_ENV_KEYS:
            env.pop(key, None)
    result["modelPicker"] = {**picker, "options": rows}
    if not available:
        return result
    for row in available:
        variant = auto_variant(row)
        overrides[variant] = row["model"]
        if isinstance(allowed, list) and allowed:
            allowed.append(variant)
    # Replacement pickers suppress Claude's native custom-model option.
    result["modelPicker"]["replaceBuiltInOptions"] = False
    return result


def picker_is_configured(settings: dict) -> bool:
    """Check the actual native source, including policy and immutable mapping."""
    picker = settings.get("modelPicker", {})
    overrides = settings.get("modelOverrides", {})
    env = settings.get("env", {})
    if not all(isinstance(value, dict) for value in (picker, overrides, env)):
        return False
    custom = env.get("ANTHROPIC_CUSTOM_MODEL_OPTION")
    if custom and (not isinstance(custom, str) or not is_auto_model(custom)):
        return False
    if picker.get("replaceBuiltInOptions", False):
        return False
    return any(
        isinstance(alias, str)
        and alias.startswith(AUTO_PREFIX)
        and isinstance(target, str)
        and not is_auto_model(target)
        and model_available(alias, settings)
        and model_available(target, settings)
        for alias, target in overrides.items()
    )


def picker_config(
    catalog: AnthropicModelCatalog,
    workspace: str,
    settings: dict | None = None,
    *,
    default_model: str | None = None,
) -> str:
    """Give the mod only configured aliases for permitted inference targets."""
    overrides = (settings or {}).get("modelOverrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
    configured = {
        target: alias
        for alias, target in overrides.items()
        if isinstance(alias, str)
        and alias.startswith(AUTO_PREFIX)
        and isinstance(target, str)
        and model_available(alias, settings or {})
        and model_available(target, settings or {})
    }
    rows = []
    for model in catalog.model_ids:
        if is_auto_model(model) or (settings is not None and model not in configured):
            continue
        label = catalog.model_id_to_display_name.get(model)
        if label:
            label = label.removeprefix("Claude ")
        if not label or label.lower() in {"opus", "sonnet", "fable", "astra", "haiku"}:
            label = re.sub(r"\[.*\]$", "", model).removeprefix("system.ai.")
            label = label.removeprefix("claude-")
            label = re.sub(r"(?<=\d)-(?=\d)", ".", label).replace("-", " ").title()
        rows.append(
            {
                "model": model,
                "label": label,
                "autoModel": configured.get(model) or auto_variant({"model": model}),
            }
        )
    if not rows:
        raise ValueError("Auto needs at least one available main model")
    env = (settings or {}).get("env", {})
    configured_default = env.get("ANTHROPIC_MODEL") if isinstance(env, dict) else None
    preferred = default_model or configured_default
    default = next((row for row in rows if preferred in (row["model"], row["autoModel"])), rows[0])
    return json.dumps(
        {
            "model": default["autoModel"],
            "mainModel": default["model"],
            "models": rows,
            "description": AUTO_DESCRIPTION,
            "storeKey": "main-model-" + hashlib.sha256(workspace.encode()).hexdigest()[:16],
        }
    )
