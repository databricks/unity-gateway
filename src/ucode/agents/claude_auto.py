"""Launch configuration for Claude's native Auto model picker."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from ucode.databricks import AnthropicModelCatalog

AUTO_MODEL = "ug-auto"
AUTO_DESCRIPTION = "Automatically picks the best model to balance quality and cost."


def model_behavior(row: dict) -> str | None:
    """Use explicit capability metadata, or the exact Claude ID behind a gateway ID.

    Static managed catalogs carry only IDs and labels. Do not infer capabilities
    from a display label or assign Claude capabilities to an unrelated model.
    """
    explicit = row.get("behavesAs")
    if isinstance(explicit, str) and explicit:
        return explicit
    model = row.get("model", "").removeprefix("system.ai.")
    if re.fullmatch(r"claude-(?:opus|sonnet|haiku|fable)-\d+(?:-\d+)*(?:\[1m\])?", model):
        return model
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


def add_picker_entry(settings: dict) -> dict:
    """Configure the real picker source, which Claude reads before loading mods.

    The alias has a real, allowed fallback even outside a UG routed launch.
    Only explicit configure may introduce it into the machine-wide file.
    """
    picker = settings.get("modelPicker")
    if not isinstance(picker, dict):
        return settings
    rows = [
        row
        for row in picker.get("options", [])
        if isinstance(row, dict)
        and isinstance(row.get("model"), str)
        and row["model"] != AUTO_MODEL
    ]
    available = [row for row in rows if model_available(row["model"], settings)]
    if not available:
        return settings
    main = next((row for row in available if "opus-5-5" in row["model"]), available[0])
    result = deepcopy(settings)
    auto = {"model": AUTO_MODEL, "label": "Auto", "description": AUTO_DESCRIPTION}
    if behavior := model_behavior(main):
        auto["behavesAs"] = behavior
    result["modelPicker"] = {**picker, "options": [auto, *rows]}
    result.setdefault("modelOverrides", {})[AUTO_MODEL] = main["model"]
    allowed = result.get("availableModels")
    if isinstance(allowed, list) and allowed and AUTO_MODEL not in allowed:
        allowed.append(AUTO_MODEL)
    return result


def picker_is_configured(settings: dict) -> bool:
    """Check the source Claude actually reads, not a merged settings-hook view."""
    target = settings.get("modelOverrides", {}).get(AUTO_MODEL)
    return (
        isinstance(target, str)
        and target != AUTO_MODEL
        and model_available(AUTO_MODEL, settings)
        and model_available(target, settings)
        and any(
            row.get("model") == AUTO_MODEL
            for row in settings.get("modelPicker", {}).get("options", [])
        )
    )


def picker_config(catalog: AnthropicModelCatalog, workspace: str) -> str:
    """Keep the virtual selection separate from every real inference model."""
    rows = []
    for model in catalog.model_ids:
        if model == AUTO_MODEL:
            continue
        label = catalog.model_id_to_display_name.get(model)
        if label:
            label = label.removeprefix("Claude ")
        if not label or label.lower() in {"opus", "sonnet", "fable", "astra", "haiku"}:
            label = re.sub(r"\[.*\]$", "", model).removeprefix("system.ai.")
            label = label.removeprefix("claude-")
            label = re.sub(r"(?<=\d)-(?=\d)", ".", label).replace("-", " ").title()
        rows.append({"model": model, "label": label})
    if not rows:
        raise ValueError("Auto needs at least one available main model")
    default = next((row for row in rows if "opus-5-5" in row["model"]), rows[0])
    return json.dumps(
        {
            "model": AUTO_MODEL,
            "mainModel": default["model"],
            "models": rows,
            "storeKey": "main-model-" + hashlib.sha256(workspace.encode()).hexdigest()[:16],
        }
    )
