"""Adapter that drives the pre-interface agent modules through :class:`Agent`.

Every ``agents/<name>.py`` module predates :mod:`ucode.agents.interface`: ug core reached into
each one directly and kept the per-agent ``if tool == ...`` branches for itself. Those branches
live here instead, keyed by the agent each :class:`LegacyAgent` wraps, so core can talk to all
of them through one interface.

This file only shrinks. As an agent grows a native class implementing :class:`Agent`, delete
its branches here and drop it from :data:`LEGACY_MODULES`; when the table is empty, so is this
module. Hooks are looked up on the wrapped module at call time rather than bound once, so an
agent can keep moving its own internals without this adapter noticing.
"""

from __future__ import annotations

from types import ModuleType

from ucode.config_io import restore_file

from . import claude, codex, copilot, gemini, opencode, pi
from .args import LaunchOptions
from .interface import ConfigureRequest, Install, Models

# The agent modules this adapter wraps, in the order ug lists agents.
LEGACY_MODULES: dict[str, ModuleType] = {
    "codex": codex,
    "claude": claude,
    "gemini": gemini,
    "opencode": opencode,
    "copilot": copilot,
    "pi": pi,
}

# Agents with their own self-updater, which ug prefers over npm when the binary is installed.
_NATIVE_UPGRADE_ARGV: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "upgrade"),
    "codex": ("codex", "update"),
}

# Agents that write the first resolved model into their ug config, so ug knows the starting
# model. Claude and Codex deliberately leave that choice to the agent unless a model is pinned.
_PINS_FIRST_MODEL = frozenset({"gemini", "opencode", "copilot", "pi"})


def model_values(value: object) -> list[str]:
    """Flatten a state model inventory (str, list, or provider-keyed dict) into model ids."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    if isinstance(value, dict):
        return [model for models in value.values() for model in model_values(models)]
    return []


class LegacyAgent:
    """One pre-interface agent module, presented as an :class:`Agent`."""

    def __init__(self, tool: str, module: ModuleType) -> None:
        self._tool = tool
        self._module = module
        self.display = str(module.SPEC["display"])
        self.install = Install(
            binary=str(module.SPEC["binary"]),
            package=str(module.SPEC["package"]),
            upgrade_argv=_NATIVE_UPGRADE_ARGV.get(tool),
            version_error=(
                self._version_error if hasattr(module, "minimum_version_error") else None
            ),
            too_new=self._too_new if hasattr(module, "too_new_downgrade") else None,
            before_install=self._detach_app_model_catalog if tool == "codex" else None,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LegacyAgent({self._tool!r})"

    # -- install ------------------------------------------------------------------------

    def _version_error(self) -> str | None:
        return self._module.minimum_version_error()

    def _too_new(self) -> tuple[str, str] | None:
        return self._module.too_new_downgrade()

    def _detach_app_model_catalog(self) -> None:
        # Codex only: drop ug's shared app catalog reference before the binary changes, so a
        # version that can no longer read it never starts against it.
        self._module.detach_app_model_catalog()

    # -- models -------------------------------------------------------------------------

    def models(self, state: dict) -> Models:
        available = tuple(dict.fromkeys(self._available_models(state)))
        return Models(available, self._default_model(state, available))

    def _available_models(self, state: dict) -> list[str]:
        # A managed static list replaces discovery outright for the agents that can have one.
        static_models = model_values(state.get(f"{self._tool}_static_models"))
        if static_models:
            return static_models
        if self._tool == "copilot":
            return model_values(state.get("copilot_models")) or (
                model_values(state.get("claude_models")) + model_values(state.get("codex_models"))
            )
        if self._tool == "pi":
            return model_values(state.get("pi_models")) or (
                model_values(state.get("claude_models"))
                + model_values(state.get("codex_models"))
                + model_values(state.get("gemini_models"))
            )
        return model_values(state.get(f"{self._tool}_models"))

    def _default_model(self, state: dict, available: tuple[str, ...]) -> str | None:
        explicit = state.get(f"{self._tool}_default_model")
        if isinstance(explicit, str) and explicit:
            return explicit
        if not available or self._tool not in _PINS_FIRST_MODEL:
            return None
        return available[0]

    # -- configure ----------------------------------------------------------------------

    def configure(self, state: dict, request: ConfigureRequest) -> dict:
        result = self._write_tool_config(state, request)
        # gemini/opencode/copilot/pi return (state, token); codex/claude return state.
        return result[0] if isinstance(result, tuple) else result

    def _write_tool_config(self, state: dict, request: ConfigureRequest) -> dict | tuple[dict, str]:
        write = self._module.write_tool_config
        if self._tool == "codex":
            return write(
                state, request.model, provider=request.provider, parent_schema=request.parent_schema
            )
        if self._tool == "claude":
            # A Model Provider Service or parent schema routes by header and discovers models
            # natively, so the usual "model required" guard doesn't apply to either Claude source.
            if not request.model and not request.provider and not request.parent_schema:
                raise RuntimeError(self._model_required_error)
            extras = request.extras
            return write(
                state,
                request.model,
                provider=request.provider,
                provider_models=extras.get("provider_models"),
                relayed=bool(extras.get("relayed")),
                route_root_model=extras.get("route_root_model"),
                custom_model=extras.get("custom_model"),
                coding_agent_config_defaults=extras.get("coding_agent_config_defaults"),
                parent_schema=request.parent_schema,
                picker_catalog=extras.get("picker_catalog"),
            )
        # Every remaining agent needs a model — including gemini under a provider,
        # which still pins the service's target model in the URL.
        if not request.model:
            raise RuntimeError(self._model_required_error)
        if self._tool == "gemini":
            return write(state, request.model, provider=request.provider)
        return write(state, request.model)

    @property
    def _model_required_error(self) -> str:
        return f"A {self._tool} model must be selected before configuration."

    # -- launch -------------------------------------------------------------------------

    def launch(self, state: dict, args: list[str], *, options: LaunchOptions) -> None:
        self._module.launch(state, args, options=options)

    # -- revert -------------------------------------------------------------------------

    def revert(self, state: dict) -> list[tuple[str, str]]:
        managed = bool((state.get("managed_configs") or {}).get(self._tool))
        spec = self._module.SPEC
        restored = restore_file(spec["config_path"], spec["backup_path"], managed)
        rows = [(f"{self.display} config", "restored" if restored else "unchanged")]
        if self._tool == "codex":
            # Older Codex (< 0.134.0) had ucode edit the shared ~/.codex/config.toml in
            # place; restoring the per-profile file above does not undo that.
            if self._module.revert_legacy_shared_config():
                rows.append((f"{self.display} shared config", "ucode entries removed"))
            rows.append(
                (f"{self.display} OS-managed settings", self._module.revert_managed_config())
            )
        if self._tool == "claude":
            rows.append(
                (f"{self.display} OS-managed settings", self._module.revert_managed_settings())
            )
        if self._tool == "pi":
            # Pi keeps its gateway providers in a second file next to the config.
            pi_restored = restore_file(
                self._module.PI_SETTINGS_PATH, self._module.PI_SETTINGS_BACKUP_PATH, managed
            )
            rows.append((f"{self.display} settings", "restored" if pi_restored else "unchanged"))
        return rows


LEGACY_AGENTS: dict[str, LegacyAgent] = {
    tool: LegacyAgent(tool, module) for tool, module in LEGACY_MODULES.items()
}
