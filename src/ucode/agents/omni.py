"""Configure and launch Omnigent with native Claude/Codex gateway providers."""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
from pathlib import Path

import typer
import yaml

from ucode.config_io import APP_DIR, ToolSpec, ensure_parent_dir, is_dry_run
from ucode.constants import AGENT_OMNI
from ucode.launcher import exec_or_spawn
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import load_state, mark_tool_managed, save_state
from ucode.ui import print_err, print_note


def launch_cli(tool_args: list[str]) -> None:
    """Forward arguments unchanged and hand over the controlling terminal."""
    executable = shutil.which("omnigent")
    if executable is None:
        print_err(
            "Omnigent is not installed. Run `ug configure --agents omni`, "
            "or uv tool install 'omnigent[databricks]>=0.22.0'."
        )
        raise typer.Exit(1)
    # Auxiliary commands (especially help and stop) must stay fast and offline.
    if not tool_args or tool_args[0] in {"start", "run", "claude", "codex"}:
        state = load_state()
        if AGENT_OMNI in (state.get("available_tools") or []):
            from ucode.skills_download import refresh_downloaded_skills_on_launch

            refresh_downloaded_skills_on_launch(state)
    try:
        exec_or_spawn([executable, *tool_args])
    except OSError as exc:
        print_err(f"Could not launch Omnigent: {exc}. Check the Omnigent installation.")
        raise typer.Exit(1) from exc


CONFIG_PATH = (
    Path(os.environ.get("OMNIGENT_CONFIG_HOME") or Path.home() / ".omnigent") / "config.yaml"
)
BACKUP_PATH = APP_DIR / "omni-config.backup.yaml"
SPEC: ToolSpec = {
    "binary": "omnigent",
    "package": "omnigent[databricks]>=0.22.0",
    "display": "Omnigent (Claude and Codex)",
    "config_path": CONFIG_PATH,
    "backup_path": BACKUP_PATH,
}
INSTALL_HINT = "Install it with `uv tool install 'omnigent[databricks]>=0.22.0'`"
_PROVIDER_NAME = "unity-gateway"
_FAMILIES = {"claude": "anthropic", "codex": "openai"}


def install_binary() -> None:
    if is_dry_run():
        print_note("Dry run: would install omnigent[databricks] with uv.")
        return
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError(f"uv is required to install Omnigent. {INSTALL_HINT}.")
    subprocess_cross_os.run([uv, "tool", "install", SPEC["package"]], check=True, timeout=300)


def default_model(state: dict) -> str | None:
    models = state.get("codex_models") or list((state.get("claude_models") or {}).values())
    return models[0] if models else None


def _default_families(entry: dict) -> set[str]:
    value = entry.get("default")
    if value is True:
        kind = entry.get("kind", "key")
        if kind == "databricks":
            return {"gemini"} if entry.get("native_gemini") else {"anthropic", "openai"}
        if kind in ("subscription", "cli-config"):
            cli = entry.get("cli")
            families = {"claude": "anthropic", "codex": "openai", "pi": "pi"}
            if cli in families:
                return {families[cli]}
        if kind == "bedrock":
            return {"anthropic"}
        families = {key for key in ("anthropic", "openai", "gemini") if key in entry}
        if kind in ("gateway", "local"):
            families.discard("gemini")  # Omnigent requires an explicit Gemini default.
        if families:
            return families
        raise RuntimeError(
            "Cannot determine an existing Omni provider's default scope. "
            "Set its explicit default family in Omnigent, then retry."
        )
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    if value is None or value is False:
        return set()
    raise RuntimeError("An Omni provider has an invalid default scope. Fix config.yaml and retry.")


def render_config(existing: dict, profile: str, families: list[str]) -> dict:
    """Select UG for the available native families, retaining unrelated providers/settings."""
    result = copy.deepcopy(existing)
    providers = result.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise RuntimeError("Omnigent's providers must be a mapping. Fix config.yaml and retry.")
    previous = providers.get(_PROVIDER_NAME)
    if previous is not None and (
        not isinstance(previous, dict) or previous.get("kind") != "databricks"
    ):
        raise RuntimeError(
            "Omnigent already has a non-Databricks 'unity-gateway' provider. "
            "Rename that provider before configuring UG."
        )
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            raise RuntimeError(f"Omnigent provider '{name}' must be a mapping.")
        if name != _PROVIDER_NAME:
            remaining = _default_families(entry) - set(families)
            # Only change the scopes UG is taking over. Credentials and other defaults stay.
            if remaining != _default_families(entry):
                entry["default"] = sorted(remaining) if remaining else False
    providers[_PROVIDER_NAME] = {"kind": "databricks", "profile": profile, "default": families}
    return result


def write_tool_config(state: dict) -> dict:
    """Omnigent resolves UG's discovered models using the named Databricks profile."""
    from ucode.agents import install_tool_binary

    profile = state.get("profile")
    if not profile:
        raise RuntimeError(
            "Omnigent requires a Databricks profile. "
            "Run `ug configure --agents omni --profile <name>`."
        )
    # Omnigent 0.22 resolves a profile's bare host and cannot retain UG's workspace selector.
    from ucode.workspace import workspace_headers

    if workspace_headers(state["workspace"]):
        raise RuntimeError(
            "Omnigent does not yet support hosts that serve multiple workspaces. "
            "Use ug claude or ug codex for this workspace."
        )
    native_tools = [tool for tool in _FAMILIES if state.get(f"{tool}_models")]
    if not native_tools:
        raise RuntimeError("No Claude or Codex models are available for Omnigent.")
    try:
        existing = (
            yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        )
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(
            f"Cannot read Omnigent config at {CONFIG_PATH}. Fix it and retry."
        ) from exc
    if existing is None:
        existing = {}
    if not isinstance(existing, dict):
        raise RuntimeError("Omnigent config.yaml must be a mapping.")
    config = render_config(existing, profile, [_FAMILIES[tool] for tool in native_tools])
    if is_dry_run():
        print_note(f"Dry run: would configure {CONFIG_PATH} for {', '.join(native_tools)}.")
        return state
    install_tool_binary(AGENT_OMNI)
    for tool in native_tools:
        install_tool_binary(tool)
    if CONFIG_PATH.exists() and not BACKUP_PATH.exists():
        ensure_parent_dir(BACKUP_PATH)
        try:
            fd = os.open(BACKUP_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(CONFIG_PATH.read_text(encoding="utf-8"))
    _write_config(config)
    state = mark_tool_managed(state, AGENT_OMNI, [["providers", _PROVIDER_NAME]])
    state["mcp_clients"] = sorted(set(state.get("mcp_clients") or []) | set(native_tools))
    save_state(state)
    print_note(
        "Omnigent configured for " + ", ".join(native_tools) + ". "
        "MCP servers and skills use those native clients. "
        "UG smart routing and managed configuration are not supported. "
        "Restart the Omnigent host when no sessions are running to load provider changes."
    )
    return state


def _write_config(config: dict) -> None:
    content = yaml.safe_dump(config, sort_keys=False)
    ensure_parent_dir(CONFIG_PATH)
    fd, temporary = tempfile.mkstemp(dir=CONFIG_PATH.parent, prefix=".ug-omni-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, CONFIG_PATH)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configured_paths(state: dict) -> list[str]:
    return [str(CONFIG_PATH)]


def launch(state: dict, tool_args: list[str], *, options: object) -> None:
    launch_cli(tool_args)


def validate_cmd(binary: str) -> list[str]:
    return [binary, "--version"]


def revert_config() -> bool:
    """Restore UG's provider/defaults while retaining host settings and later user edits."""
    if not CONFIG_PATH.exists():
        return False
    try:
        current = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        original = (
            yaml.safe_load(BACKUP_PATH.read_text(encoding="utf-8")) if BACKUP_PATH.exists() else {}
        )
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(
            "Cannot read Omnigent config for revert; fix the YAML and retry."
        ) from exc
    if not isinstance(current, dict) or not isinstance(original or {}, dict):
        return False
    providers = current.get("providers")
    owned = providers.get(_PROVIDER_NAME) if isinstance(providers, dict) else None
    if not isinstance(owned, dict) or owned.get("kind") != "databricks":
        return False
    original = original or {}
    old_providers = original.get("providers") or {}
    expected = render_config(original, owned.get("profile", ""), sorted(_default_families(owned)))
    for name, entry in providers.items():
        old = old_providers.get(name)
        written = expected["providers"].get(name)
        if (
            name == _PROVIDER_NAME
            or not isinstance(entry, dict)
            or not isinstance(old, dict)
            or not isinstance(written, dict)
        ):
            continue
        if entry.get("default") == written.get("default"):
            if "default" in old:
                entry["default"] = old["default"]
            else:
                entry.pop("default", None)
    if _PROVIDER_NAME in old_providers:
        providers[_PROVIDER_NAME] = old_providers[_PROVIDER_NAME]
    else:
        providers.pop(_PROVIDER_NAME)
    _write_config(current)
    BACKUP_PATH.unlink(missing_ok=True)
    return True
