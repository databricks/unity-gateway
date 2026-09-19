"""Build managed Codex catalogs offline, using the installed harness's schema and prompts."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from ucode.codex_config import ModelVisibility
from ucode.smart_routing.codex_routing import codex_model_id
from ucode.ui import print_warning

_TIMEOUT_SECONDS = 30

# Known hosted-model capabilities, following universe#2591694. These are not
# universal non-GPT defaults: an arbitrary UC service need not support images,
# a million-token context, or the same reasoning efforts.
_HOSTED_CAPABILITIES = {
    "glm-5-2": (["text"], ["high", "max"]),
    "glm-5-3": (["text"], ["low", "high", "max"]),
    "glm-5-3-flash": (["text", "image"], ["low", "high", "max"]),
    "kimi-k3": (["text", "image"], ["low", "high", "max"]),
}
_HOSTED_DEFAULTS = {
    "input_modalities": ["text"],
    "context_window": 32768,
    "max_context_window": 32768,
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
    return codex_model_id(slug).removeprefix("system.ai.").replace(".", "-")


def build_codex_catalog(
    bundled_models: list[dict],
    names: list[str],
    *,
    warn: Callable[[str], None] | None = None,
) -> dict:
    """Copy native presets or derive generic compatibility defaults, in admin order.

    Preserve all native metadata (including unknown future fields), except picker
    identity/visibility/order. Only requested slugs are visible. Hidden aliases
    for selected GPTs support the smart router's native-ID translation.
    """
    by_slug = {model["slug"]: model for model in bundled_models}
    by_key = {_model_key(slug): model for slug, model in by_slug.items()}
    models = []
    for name in dict.fromkeys(names):
        native = by_slug.get(name) or by_key.get(_model_key(name))
        if native is not None:
            model = copy.deepcopy(native)
        else:
            if _model_key(name).startswith(("gpt-", "databricks-gpt-")):
                if warn is not None:
                    warn(
                        f"Codex is missing metadata for managed GPT model '{name}', so UG is "
                        "falling back to default metadata. Try updating Codex with "
                        "`ug codex update`."
                    )
            baseline = next((m for m in bundled_models if m.get("tool_mode") is None), None)
            if baseline is None:
                raise RuntimeError(
                    f"Codex has no ordinary-tool bundled model to base '{name}' on. "
                    "Upgrade or reinstall Codex."
                )
            model = copy.deepcopy(baseline)
            model.update(copy.deepcopy(_HOSTED_DEFAULTS))
            capabilities = _HOSTED_CAPABILITIES.get(name.removeprefix("system.ai."))
            if capabilities:
                modalities, efforts = capabilities
                model.update(
                    input_modalities=list(modalities),
                    context_window=1048576,
                    max_context_window=1048576,
                    default_reasoning_level=efforts[-1],
                    supported_reasoning_levels=[
                        {"effort": effort, "description": f"{effort.capitalize()} reasoning"}
                        for effort in efforts
                    ],
                )
            model["description"] = f"{name} served on the Databricks AI Gateway."
        model.update(
            slug=name,
            display_name=name,
            visibility=ModelVisibility.LIST,
            supported_in_api=True,
            priority=len(models),
        )
        models.append(model)
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


def prepare_codex_catalog(binary: str, names: list[str]) -> dict:
    """Extract and validate with the launch binary; never fetch or reuse stale metadata.

    --bundled bypasses discovery. Validation reads an explicit local catalog.
    Both commands run outside user/project config in a disposable CODEX_HOME.
    Failure blocks configuration rather than falling back to remote discovery.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="ug-codex-catalog-") as home:
            env = {**os.environ, "CODEX_HOME": home}

            def run(args: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [binary, *args],
                    env=env,
                    cwd=home,
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT_SECONDS,
                    check=True,
                )

            result = run(["debug", "models", "--bundled"])
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
            fallback_warnings: list[str] = []
            catalog = build_codex_catalog(bundled, names, warn=fallback_warnings.append)
            candidate = Path(home) / "catalog.json"
            candidate.write_text(json.dumps(catalog), encoding="utf-8")
            run(
                [
                    "-c",
                    f"model_catalog_json={json.dumps(str(candidate))}",
                    "debug",
                    "models",
                ]
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "Could not build the managed Codex model catalog locally. "
            "Upgrade the active Codex installation and verify "
            "`codex debug models --bundled` works, then retry configuration. "
            "Gateway discovery was not used."
        ) from exc
    for message in fallback_warnings:
        print_warning(message)
    return catalog
