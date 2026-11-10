"""Claude model-source discovery and native picker presentation."""

from __future__ import annotations

import re

from ucode import databricks
from ucode.smart_routing.routing import unwrap_anthropic_gateway_model
from ucode.string_utils import model_picker_label


def picker_label(model: str) -> str:
    """Hide a provider service's qualifier in Claude picker labels."""
    if ".claude-" in model:
        model = "claude-" + model.rpartition(".claude-")[2]
    return model_picker_label(model)


def source_models(
    workspace: str,
    token: str,
    *,
    parent_schema: str | None = None,
    provider: str | None = None,
) -> tuple[list[str], str | None]:
    catalog = databricks.list_anthropic_model_catalog(
        workspace, token, parent_schema=parent_schema, provider=provider
    )
    reason = catalog.error_msg
    if provider and reason == databricks._NO_ANTHROPIC_MODEL_IDS:
        reason = None
    return catalog.model_ids, reason


_CLAUDE_PICKER_LABEL_RE = re.compile(
    r"^Claude (Fable|Opus|Sonnet|Haiku) (\d+(?:\.\d+)*)$", re.IGNORECASE
)


def static_picker_option(model: str) -> dict[str, str]:
    display_model = unwrap_anthropic_gateway_model(model)
    option = {
        "model": databricks.maybe_add_gpt_1m_suffix(model),
        "label": picker_label(display_model),
    }
    if display_model != model:
        option["description"] = f"Managed by your organization ({display_model})"
    return option


def picker_option(model: str, label: str, description: str | None = None) -> dict[str, str]:
    option = {"model": model, "label": label}
    if not description and (display_model := unwrap_anthropic_gateway_model(model)) != model:
        description = f"Managed by your organization ({display_model})"
    if description:
        option["description"] = description
    match = _CLAUDE_PICKER_LABEL_RE.fullmatch(label)
    if match:
        family, version = match.groups()
        option["behavesAs"] = f"claude-{family.lower()}-{version.replace('.', '-')}"
    return option
