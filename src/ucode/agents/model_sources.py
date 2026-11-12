"""Expand managed schema and provider wildcards into model IDs."""

from collections.abc import Callable

from ucode.agents.copilot import build_copilot_catalog
from ucode.codex_config import catalog_slugs
from ucode.databricks import (
    CodexCatalogSource,
    _fetch_codex_model_catalog,
    get_model_provider_service,
    list_anthropic_model_catalog,
    list_model_service_entries,
    list_provider_service_models,
)
from ucode.ui import print_warning


def _claude_schema_models(workspace: str, token: str, schema: str) -> tuple[list[str], str | None]:
    catalog = list_anthropic_model_catalog(workspace, token, parent_schema=schema)
    return catalog.model_ids, catalog.error_msg


def _codex_schema_models(workspace: str, token: str, schema: str) -> tuple[list[str], str | None]:
    catalog = _fetch_codex_model_catalog(
        workspace, token, source=CodexCatalogSource.PARENT_SCHEMA, identifier=schema
    )
    return catalog_slugs(catalog), None


def _copilot_schema_models(workspace: str, token: str, schema: str) -> tuple[list[str], str | None]:
    entries, reason = list_model_service_entries(workspace, token, parent_schema=schema)
    return [model.id for model in build_copilot_catalog(entries)], reason


SCHEMA_MODEL_CATALOGS: dict[str, Callable[[str, str, str], tuple[list[str], str | None]]] = {
    "claude": _claude_schema_models,
    "codex": _codex_schema_models,
    "copilot": _copilot_schema_models,
}


def is_model_source_wildcard(source: str) -> bool:
    parts = source.split(".")
    return len(parts) in (3, 4) and parts[-1] == "*" and all(p and "*" not in p for p in parts[:-1])


def _expand_wildcard_source(
    source: str, tool: str, workspace: str, token: str
) -> tuple[list[str], str | None]:
    source_name = source[:-2]
    if len(source.split(".")) == 3:
        return SCHEMA_MODEL_CATALOGS[tool](workspace, token, source_name)
    service, reason = get_model_provider_service(source_name, workspace, token)
    if service is None:
        return [], reason
    targets, reason = list_provider_service_models(
        workspace, token, source_name, service.get("provider_type", "")
    )
    return [
        target if target.startswith(f"{source_name}.") else f"{source_name}.{target}"
        for target in targets
    ], reason


def expand_wildcard_sources(
    sources: list[str],
    tool: str,
    workspace: str,
    token: str,
    *,
    fallbacks: dict[str, list[str]] | None = None,
) -> tuple[list[str], dict[str, list[str]]]:
    expanded: list[str] = []
    lookups: dict[str, list[str]] = {}
    live: dict[str, list[str]] = {}
    for source in sources:
        if not is_model_source_wildcard(source):
            expanded.append(source)
            continue
        if source not in lookups:
            try:
                targets, reason = _expand_wildcard_source(source, tool, workspace, token)
            except RuntimeError as exc:
                targets, reason = [], str(exc)
            if targets:
                live[source] = list(targets)
            else:
                detail = f" ({reason})" if reason else ""
                prefix = f"Could not expand managed model source {source}{detail}; "
                fallback = (fallbacks or {}).get(source)
                if fallback:
                    targets = fallback
                    print_warning(prefix + "using the list from the last configure.")
                else:
                    print_warning(
                        prefix + "check service access and compatible models, then retry."
                    )
            lookups[source] = targets
        expanded.extend(lookups[source])
    return list(dict.fromkeys(expanded)), live
