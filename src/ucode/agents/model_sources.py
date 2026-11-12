"""Expand managed schema and provider wildcards into model IDs."""

from typing import Protocol

from ucode.agents import claude, codex, copilot
from ucode.databricks import list_model_provider_service_targets
from ucode.ui import print_warning


class ModelSourceAgent(Protocol):
    def source_models(
        self,
        workspace: str,
        token: str,
        *,
        parent_schema: str | None = None,
    ) -> tuple[list[str], str | None]: ...


SOURCE_MODEL_AGENTS: dict[str, ModelSourceAgent] = {
    "claude": claude,
    "codex": codex,
    "copilot": copilot,
}


def is_model_source_wildcard(source: str) -> bool:
    parts = source.split(".")
    return len(parts) in (3, 4) and parts[-1] == "*" and all(p and "*" not in p for p in parts[:-1])


def _expand_wildcard_source(
    source: str, tool: str, workspace: str, token: str
) -> tuple[list[str], str | None]:
    source_name = source[:-2]
    if len(source.split(".")) == 3:
        return SOURCE_MODEL_AGENTS[tool].source_models(workspace, token, parent_schema=source_name)
    return list_model_provider_service_targets(workspace, token, source_name)


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
