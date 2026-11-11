"""Publish Windows Desktop policy without taking ownership of unrelated registry values."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from ucode import managed_files
from ucode.os_compatibility import system_cross_os, windows_machine_paths

POLICY_KEY = r"SOFTWARE\Policies\Claude"
POLICY_NAMES = frozenset(
    "inferenceProvider inferenceGatewayBaseUrl inferenceGatewayAuthScheme "
    "inferenceCredentialKind inferenceCredentialHelper inferenceCredentialHelperArgs "
    "inferenceCredentialHelperTtlSec inferenceCredentialHelperTimeoutSec "
    "inferenceCredentialHelperSilentRefreshEnabled modelDiscoveryEnabled toolSearchEnabled "
    "inferenceModels inferenceCustomHeaders claudeAiImport managedMcpServers "
    "otlpEndpoint otlpProtocol otlpHeadersHelper otlpTracesEnabled".split()
)


def _registry():
    import winreg

    return winreg


def read_policy() -> dict[str, tuple[object, int]]:
    registry = _registry()
    try:
        key = registry.OpenKey(
            registry.HKEY_LOCAL_MACHINE, POLICY_KEY, 0, registry.KEY_READ | registry.KEY_WOW64_64KEY
        )
    except FileNotFoundError:
        return {}
    with key:
        return {
            name: (value, kind)
            for name, value, kind in (
                registry.EnumValue(key, index) for index in range(registry.QueryInfoKey(key)[1])
            )
        }


def _value(values: dict, name: str):
    return next((value for key, value in values.items() if key.casefold() == name.casefold()), None)


def _write_policy(changes: Mapping[str, tuple[object, int] | None]) -> None:
    registry = _registry()
    access = registry.KEY_SET_VALUE | registry.KEY_WOW64_64KEY
    if any(value is not None for value in changes.values()):
        key = registry.CreateKeyEx(registry.HKEY_LOCAL_MACHINE, POLICY_KEY, 0, access)
    else:
        try:
            key = registry.OpenKey(registry.HKEY_LOCAL_MACHINE, POLICY_KEY, 0, access)
        except FileNotFoundError:
            return
    with key:
        for name, value in changes.items():
            if value is None:
                try:
                    registry.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                data, kind = value
                registry.SetValueEx(key, name, 0, kind, data)


def _ownership_path() -> Path:
    return managed_files.windows_desktop_mdm_root() / "ownership.json"


def _read_ownership() -> dict | None:
    path = _ownership_path()
    windows_machine_paths.validate(path)
    try:
        state = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or state.get("version") != 1:
        raise RuntimeError(f"Invalid Desktop ownership record: {path}")
    for field in ("registry", "pending_registry"):
        values = state.get(field)
        if not isinstance(values, dict) or any(
            name not in POLICY_NAMES
            or not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[0], str)
            or value[1] != 1
            for name, value in values.items()
        ):
            raise RuntimeError(f"Invalid Desktop registry ownership in {path}")
    return state


def _write_ownership(state: dict) -> None:
    managed_files.reconcile_privileged_files(
        {_ownership_path(): json.dumps(state, indent=2, sort_keys=True) + "\n"},
        display="Claude Desktop",
    )


def _owned_policy(state: dict, current: dict) -> dict:
    previous = state["registry"]
    pending = state["pending_registry"]
    owned = {}
    for name in previous.keys() | pending.keys():
        value = _value(current, name)
        if value is None:
            continue
        if list(value) not in (previous.get(name), pending.get(name)):
            raise RuntimeError(
                f"Desktop policy {name} changed outside UG. Restore it or resolve ownership before retrying."
            )
        owned[name] = value
    return owned


def publish(source: Path, files: dict[Path, bytes | None], policy: dict[str, str]) -> None:
    if not system_cross_os.is_admin():
        raise PermissionError("Run ug configure --global from an elevated terminal.")
    if set(policy) - POLICY_NAMES:
        raise RuntimeError("Unexpected Desktop policy fields.")
    plugin = managed_files.claude_desktop_org_plugin_path()
    if plugin is None:
        raise RuntimeError("Windows organization-plugin path is unavailable.")
    for path in (*files, plugin):
        windows_machine_paths.validate(path)
    current = read_policy()
    state = _read_ownership()
    if state is None:
        if any(path.exists() for path in (*files, plugin)):
            raise RuntimeError("Existing Desktop runtime/plugin has no UG ownership record.")
        state = {"version": 1, "registry": {}, "pending_registry": {}}
    owned = _owned_policy(state, current)
    for name in POLICY_NAMES:
        if _value(current, name) is not None and name not in owned:
            raise RuntimeError(f"Existing Desktop policy {name} belongs to another deployment.")
    desired = {name: (value, 1) for name, value in policy.items()}
    # A failed publication remains attributable across users and can be repaired by configure.
    _write_ownership({"version": 1, "registry": owned, "pending_registry": desired})
    managed_files.reconcile_privileged_files(
        {path: data.decode("utf-8") if data is not None else None for path, data in files.items()},
        display="Claude Desktop",
    )
    managed_files.replace_privileged_bundle(plugin, source)
    changes = {name: desired.get(name) for name in owned.keys() | desired.keys()}
    rollback = {name: _value(current, name) for name in changes}
    try:
        _write_policy(changes)
    except OSError:
        _write_policy(rollback)
        raise
    _write_ownership({"version": 1, "registry": desired, "pending_registry": {}})


def remove() -> bool:
    if not _ownership_path().exists():
        return False
    state = _read_ownership()
    assert state is not None
    if not system_cross_os.is_admin():
        raise managed_files.ManagedFileWriteUnavailable("administrator rights are required")
    owned = _owned_policy(state, read_policy())
    _write_policy(dict.fromkeys(owned))
    plugin = managed_files.claude_desktop_org_plugin_path()
    if plugin is not None:
        managed_files.replace_privileged_bundle(plugin, None)
    files = managed_files.windows_desktop_mdm_files() - {_ownership_path()}
    managed_files.reconcile_privileged_files(dict.fromkeys(files), display="Claude Desktop")
    managed_files.reconcile_privileged_files({_ownership_path(): None}, display="Claude Desktop")
    root = managed_files.windows_desktop_mdm_root()
    if not any(root.iterdir()):
        managed_files.remove_privileged_tree(root, empty_only=True)
    return True
