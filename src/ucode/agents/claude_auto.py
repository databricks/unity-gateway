"""Fixed native model identities behind Claude's single Auto picker row."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
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


def auto_model_target(alias: str) -> str | None:
    """Recover a launch-only alias only when its complete identity verifies.

    Personal aliases retain the target ID (Claude IDs omit system.ai in their
    capability suffix). Admin behavesAs aliases still require their explicit mapping.
    Native may strip a trailing [1m]; the digest still covers the full target.
    """
    match = re.fullmatch(r"ug-auto-[a-f0-9]{12}-(.+)", alias)
    if match:
        suffix = match[1]
        for candidate in (suffix, suffix + "[1m]"):
            for target in (candidate, "system.ai." + candidate):
                expected = auto_variant({"model": target})
                if alias in (expected, expected.removesuffix("[1m]")):
                    return target
    return None


def model_available(model: str, settings: dict) -> bool:
    """Conservatively keep exact configured targets, including context suffixes."""

    def matches(entry: object) -> bool:
        return isinstance(entry, str) and entry in (model, model.split("[", 1)[0])

    allowed = settings.get("availableModels")
    denied = settings.get("deniedModels")
    return (not allowed or any(matches(entry) for entry in allowed)) and not (
        isinstance(denied, list) and any(matches(entry) for entry in denied)
    )


def remove_auto_configuration(
    settings: dict,
    *,
    alias_targets: Mapping[str, str] | None = None,
    model_policy: dict | None = None,
) -> dict:
    """Remove UG Auto metadata while retaining permitted real-model selections."""
    result = deepcopy(settings)
    overrides = settings.get("modelOverrides")
    overrides = overrides if isinstance(overrides, dict) else {}

    def resolve(alias: str) -> str | None:
        target = None
        for source in (alias_targets or {}, overrides):
            for key in (alias, alias + "[1m]"):
                if key in source:
                    target = source[key]
                    break
            else:
                continue
            break
        else:
            target = auto_model_target(alias)
        if (
            isinstance(target, str)
            and target.strip()
            and not is_auto_model(target)
            and model_available(target, {"deniedModels": settings.get("deniedModels")})
            and model_available(target, model_policy or {})
        ):
            return target
        return None

    def is_alias(value: object) -> bool:
        return isinstance(value, str) and is_auto_model(value)

    if isinstance(result.get("modelOverrides"), dict):
        result["modelOverrides"] = {
            key: value for key, value in result["modelOverrides"].items() if not is_alias(key)
        }
    picker = result.get("modelPicker")
    if isinstance(picker, dict) and isinstance(picker.get("options"), list):
        picker["options"] = [
            row
            for row in picker["options"]
            if not (isinstance(row, dict) and is_alias(row.get("model")))
        ]
    allowed = result.get("availableModels")
    if isinstance(allowed, list) and any(is_alias(model) for model in allowed):
        remaining = [model for model in allowed if not is_alias(model)]
        if not remaining:
            remaining = list(
                dict.fromkeys(target for alias in allowed if (target := resolve(alias)))
            )
            if not remaining:
                raise ValueError(
                    "Cannot remove Claude Auto aliases without making availableModels "
                    "unrestricted. Configure an explicit permitted real-model list, then retry."
                )
        result["availableModels"] = remaining

    def repair_selection(document: dict, key: str) -> None:
        value = document.get(key)
        if isinstance(value, str) and is_auto_model(value):
            target = resolve(value)
            if target and model_available(target, result):
                document[key] = target
            else:
                document.pop(key)

    repair_selection(result, "model")
    env = result.get("env")
    if isinstance(env, dict):
        if is_alias(env.get("ANTHROPIC_CUSTOM_MODEL_OPTION")):
            for key in AUTO_CUSTOM_MODEL_ENV_KEYS:
                env.pop(key, None)
        for key in (
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_MODEL",
            "ANTHROPIC_SMALL_FAST_MODEL",
            *(
                f"ANTHROPIC_DEFAULT_{family}_MODEL"
                for family in ("OPUS", "SONNET", "HAIKU", "FABLE")
            ),
        ):
            repair_selection(env, key)
    return result


def add_picker_entry(settings: dict, *, static_auto_row: bool = False) -> dict:
    """Install immutable aliases and one native custom row at explicit configure.

    Aliases are not modelPicker rows: identical labels would create duplicate rows.
    The mod changes only the process-local custom option, never shared profiles.

    With ``static_auto_row`` (a managed static catalog under the Auto config
    version), the picker keeps ``replaceBuiltInOptions`` as the catalog wrote it
    and puts a single Auto row for the default variant first instead of exposing the
    custom option. The mod's in-process custom option still drives main-model
    switching; aliases stay in ``modelOverrides`` and ``availableModels``.
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
    if static_auto_row:
        # One Auto row, for the same default variant picker_config prefers.
        env_values = result.get("env")
        preferred = env_values.get("ANTHROPIC_MODEL") if isinstance(env_values, dict) else None
        default_row = next(
            (row for row in available if preferred in (row["model"], auto_variant(row))),
            available[0],
        )
        # Auto leads UG's rows; Claude always renders its native Default row first.
        result["modelPicker"]["options"] = [
            {
                "model": auto_variant(default_row),
                "label": "Auto",
                "description": AUTO_DESCRIPTION,
            },
            *rows,
        ]
    else:
        # Replacement pickers suppress Claude's native custom-model option.
        result["modelPicker"]["replaceBuiltInOptions"] = False
    return result


def picker_has_auto_row(settings: dict) -> bool:
    """Whether a replacement picker already carries the single static Auto row."""
    picker = settings.get("modelPicker")
    if not isinstance(picker, dict) or picker.get("replaceBuiltInOptions", False) is not True:
        return False
    options = picker.get("options")
    return isinstance(options, list) and any(
        isinstance(row, dict) and isinstance(row.get("model"), str) and is_auto_model(row["model"])
        for row in options
    )


def static_auto_model(settings: dict) -> str | None:
    """The alias of a replacement picker's single static Auto row, if it has one."""
    if not picker_has_auto_row(settings):
        return None
    return next(
        row["model"]
        for row in settings["modelPicker"]["options"]
        if isinstance(row, dict)
        and isinstance(row.get("model"), str)
        and is_auto_model(row["model"])
    )


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
    if picker.get("replaceBuiltInOptions", False) and not picker_has_auto_row(settings):
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
