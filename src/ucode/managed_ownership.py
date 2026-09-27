"""Destination ownership and recoverable application over the managed-backups store.

Agent adapters supply pure composition plans. The transaction owns all reads, locks,
backups and writes, including cleanup of destinations omitted by the next source.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import tomlkit

from ucode import managed_files as files
from ucode.child_env import agent_custom_env, env_name_identity, validate_env_name
from ucode.config_io import is_dry_run
from ucode.managed_source import SelectedManagedSource
from ucode.native_settings import agent_native_requirements, agent_native_settings
from ucode.ui import print_warning

OwnedPaths = list[list[str]] | Callable[[dict, dict], list[list[str]]]


@dataclass
class DestinationPlan:
    target: str
    path: Path
    parser: files.ManagedParser
    dumper: files.ManagedDumper
    compose: Callable[[dict], dict]
    owned_paths: OwnedPaths
    contributions: dict[tuple[str, ...], list] = field(default_factory=dict)
    exact_array_paths: frozenset[tuple[str, ...]] = frozenset()
    privileged: bool = False
    optional: bool = False
    compatible: Callable[[dict, dict], bool] | None = None
    compatible_scope: str = "local-compatible"
    writable: bool = True
    baseline_text: str | None = None
    baseline_supplied: bool = False
    delete_when_empty: bool = False
    generated_artifact: bool = False
    legacy_owned_paths: list[list[str]] = field(default_factory=list)
    legacy_effects: list[dict] = field(default_factory=list)


def leaf_paths(value: dict, prefix: list[str] | None = None) -> list[list[str]]:
    """Declare leaves, keeping arrays as contribution sets rather than numeric paths."""
    paths = []
    for key, child in value.items():
        path = [*(prefix or []), key]
        if isinstance(child, dict):
            paths.extend(leaf_paths(child, path))
        else:
            paths.append(path)
    return paths


def applied_source(agent: str) -> dict | None:
    manifest = files._load_manifest()
    record = manifest.get("applications", {}).get(agent)
    pending = manifest.get("pending", {}).get(agent)
    if pending:
        return {**(record or {}), "status": "pending", "pending_source": pending.get("source")}
    return deepcopy(record) if isinstance(record, dict) else None


def source_for_writer(
    state: dict, agent: str, selected_source: SelectedManagedSource | None
) -> SelectedManagedSource | None:
    if selected_source is not None:
        selected_source.check_target(state["workspace"], agent)
        return selected_source
    application = applied_source(agent)
    enrolled = (
        bool(agent_custom_env(state, agent))
        or bool(agent_native_settings(state, agent))
        or bool(agent_native_requirements(state, agent))
        or application is not None
        or any(
            entry.get("agent") == agent and "active_effects" in entry
            for entry in files._manifest_files(files._load_manifest()).values()
        )
    )
    if not enrolled:
        return None
    from ucode.managed_config import refresh_managed_config

    return SelectedManagedSource.from_api(refresh_managed_config(state), state["workspace"], agent)


def retired_environment(agent: str) -> set[str]:
    """Names to remove from a child environment, without modifying the parent process."""
    return set(files._load_manifest().get("retired_env", {}).get(agent, {}))


def _canonical(value: object) -> str:
    return json.dumps(files._unwrap(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _remove_elements(current: list, supplied: list) -> list:
    result = deepcopy(current)
    for element in supplied:
        # Hook matcher groups may gain user handlers after adoption. Match the group
        # metadata separately and remove only the previously supplied handlers.
        if isinstance(element, dict) and isinstance(element.get("hooks"), list):
            metadata = {key: value for key, value in element.items() if key != "hooks"}
            for index, candidate in enumerate(result):
                if not isinstance(candidate, dict) or not isinstance(candidate.get("hooks"), list):
                    continue
                if _canonical(
                    {key: value for key, value in candidate.items() if key != "hooks"}
                ) != _canonical(metadata):
                    continue
                remainder = _remove_elements(candidate["hooks"], element["hooks"])
                if remainder:
                    candidate["hooks"] = remainder
                else:
                    result.pop(index)
                break
            continue
        identity = _canonical(element)
        for index, candidate in enumerate(result):
            if _canonical(candidate) == identity:
                result.pop(index)
                break
    return result


def _add_elements(current: list, supplied: list) -> list:
    result = deepcopy(current)
    for element in supplied:
        if isinstance(element, dict) and isinstance(element.get("hooks"), list):
            metadata = {key: value for key, value in element.items() if key != "hooks"}
            for candidate in result:
                if (
                    isinstance(candidate, dict)
                    and isinstance(candidate.get("hooks"), list)
                    and _canonical(
                        {key: value for key, value in candidate.items() if key != "hooks"}
                    )
                    == _canonical(metadata)
                ):
                    candidate["hooks"].extend(deepcopy(element["hooks"]))
                    break
            else:
                result.append(deepcopy(element))
        else:
            result.append(deepcopy(element))
    return result


def _remove_effects(doc: dict, effects: list[dict]) -> None:
    for effect in effects:
        path = effect["path"]
        if "elements" in effect:
            current = files._path_value(doc, path)
            if isinstance(current, list):
                remaining = _remove_elements(current, effect["elements"])
                if remaining:
                    files._set_path_value(doc, path, remaining)
                else:
                    files._delete_path_value(doc, path)
        else:
            files._delete_path_value(doc, path)


def _merge_effects(previous: list[dict], pending: list[dict]) -> list[dict]:
    merged = {tuple(effect["path"]): deepcopy(effect) for effect in previous}
    for effect in pending:
        path = tuple(effect["path"])
        before = merged.get(path)
        if before and "elements" in before and "elements" in effect:
            additions = _remove_elements(effect["elements"], before["elements"])
            before["elements"].extend(additions)
        else:
            merged[path] = deepcopy(effect)
    return list(merged.values())


def _effect_drifted(current: dict, effect: dict) -> bool:
    value = files._path_value(current, effect["path"])
    if "elements" in effect:
        return not isinstance(value, list) or bool(_remove_elements(effect["elements"], value))
    return not files.is_semantically_equal(value, effect.get("value"))


def _effects(plan: DestinationPlan, base: dict, desired: dict) -> list[dict]:
    paths = (
        plan.owned_paths if isinstance(plan.owned_paths, list) else plan.owned_paths(base, desired)
    )
    result: list[dict] = []
    for path in paths:
        if not files._owned_path(path):
            raise RuntimeError(f"Invalid owned field path for {plan.target}.")
        value = files._path_value(desired, path)
        if value is files._MISSING:
            continue
        if isinstance(value, dict):
            raise RuntimeError(
                f"Ownership must declare leaf fields at {plan.target}.{'.'.join(path)}."
            )
        effect = {"path": list(path), "value": files._unwrap(value)}
        if isinstance(value, list):
            effect["elements"] = deepcopy(plan.contributions.get(tuple(path), value))
        result.append(effect)
    for index, effect in enumerate(result):
        path = effect["path"]
        for other in result[index + 1 :]:
            other_path = other["path"]
            if (
                path == other_path
                or path == other_path[: len(path)]
                or other_path == path[: len(other_path)]
            ):
                raise RuntimeError(f"Overlapping owned fields at {plan.target}.{'.'.join(path)}.")
    return result


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    if is_dry_run():
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlinked ownership lock {path}.")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _check_path(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"Refusing non-regular managed destination {path}.")
    for parent in path.parents:
        if parent.is_symlink():
            raise RuntimeError(f"Refusing managed destination under symlink {parent}.")


def _read(plan: DestinationPlan) -> tuple[str | None, dict]:
    _check_path(plan.path)
    text = files.read_managed_file(plan.path)
    try:
        document = plan.parser(text) if text is not None else {}
    except Exception as exc:
        raise RuntimeError(
            f"Cannot parse {plan.target} at {plan.path}; repair it before retrying."
        ) from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"Expected an object at {plan.path}.")
    return text, document


def _atomic_replace(path: Path, text: str | None, expected: str | None) -> None:
    _check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        if files.read_managed_file(path) != expected:
            raise RuntimeError(f"Concurrent edit at {path}; preserved the newer file. Retry.")
        path.unlink(missing_ok=True)
        return
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staging = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(staging, path.stat().st_mode & 0o777)
        if files.read_managed_file(path) != expected:
            raise RuntimeError(f"Concurrent edit at {path}; preserved the newer file. Retry.")
        os.replace(staging, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        staging.unlink(missing_ok=True)


def _registered_plan(agent: str, target: str, path: Path) -> DestinationPlan:
    """Resolve recorded paths through code-owned surfaces before cleanup or release."""
    from ucode.agents import claude, codex

    if agent == "claude":
        paths = {
            "private_settings": {claude.CLAUDE_SETTINGS_PATH},
            "user_settings": {claude.CLAUDE_USER_SETTINGS_PATH},
            "managed_settings": {claude._managed_settings_path()},
            "web_search_mcp": {claude.claude_mcp_config_path()},
        }
        parser, dumper = claude._parse_managed_settings, claude._dump_managed_settings
    elif agent == "codex":
        paths = {
            "private_settings": {codex.CODEX_CONFIG_PATH},
            "user_settings": {codex.LEGACY_CODEX_CONFIG_PATH, codex._legacy_config_path()},
            "managed_settings": {codex.codex_managed_config_path()},
            "requirements": {codex.codex_requirements_path()},
            "model_catalog": {codex.CODEX_MODEL_CATALOG_PATH},
        }
        parser, dumper = codex._parse_managed_config, tomlkit.dumps
        if target in {"model_catalog", "scoped_model_catalog"}:
            parser, dumper = json.loads, lambda value: json.dumps(value, indent=2) + "\n"
        if target == "scoped_model_catalog":
            base = codex.CODEX_MODEL_CATALOG_PATH
            if path.parent == base.parent and re.fullmatch(
                re.escape(base.stem) + r"-[0-9a-f]{16}" + re.escape(base.suffix), path.name
            ):
                paths[target] = {path}
    else:
        raise RuntimeError(f"Managed ownership is not supported for {agent}.")
    if path not in paths.get(target, set()):
        raise RuntimeError(
            f"Unrecognized {agent} destination {target} at {path}; repair ownership metadata."
        )
    return DestinationPlan(
        target,
        path,
        parser,
        dumper,
        lambda doc: doc,
        [],
        privileged=target in {"managed_settings", "requirements"},
        delete_when_empty=target in {"model_catalog", "scoped_model_catalog"},
    )


def _backup(manifest: dict, plan: DestinationPlan, agent: str, current: str | None) -> dict:
    key = files._destination_key(agent, plan.path, plan.target)
    entry = files._manifest_files(manifest).get(key)
    if entry is not None:
        files._original_text(entry)
        return entry
    original = plan.baseline_text if plan.baseline_supplied else current
    entry = {
        "agent": agent,
        "scope": plan.target,
        "path": str(plan.path),
        "original_existed": original is not None,
        "owned_paths": [],
    }
    if original is not None:
        filename = f"{key}.backup{plan.path.suffix}"
        files._write_private_file(files.MANAGED_BACKUP_DIR / filename, original)
        entry.update(backup_file=filename, original_sha256=files._sha256(original))
    files._manifest_files(manifest)[key] = entry
    return entry


def _legacy_effects(entry: dict, parser: files.ManagedParser) -> list[dict]:
    last_text = files._snapshot_text(entry, "last_applied_file")
    if last_text is None:
        return []
    last = parser(last_text)
    original_text = files._original_text(entry)
    original = parser(original_text) if original_text is not None else {}
    result = {}
    for parent in entry.get("owned_paths", []):
        if not files._owned_path(parent) or parent[0] in {"managedMcpServers", "mcp_servers"}:
            continue
        value = files._path_value(last, parent)
        paths = leaf_paths(value, parent) if isinstance(value, dict) else [parent]
        for path in paths:
            value = files._path_value(last, path)
            before = files._path_value(original, path)
            if value is files._MISSING or files.is_semantically_equal(value, before):
                continue
            effect = {"path": path, "value": files._unwrap(value)}
            if isinstance(value, list):
                effect["elements"] = _remove_elements(
                    value, before if isinstance(before, list) else []
                )
            result[tuple(path)] = effect
    return list(result.values())


def _source_record(source: SelectedManagedSource, owner: str) -> dict:
    return {
        "kind": source.kind,
        "workspace": source.workspace,
        "agent": source.agent,
        "owner": owner,
        "path": str(source.resolved_path) if source.resolved_path else None,
        "digest": source.digest,
        "manifest": source.manifest,
        "status": "applied",
    }


def preflight_source_transition(source: SelectedManagedSource) -> None:
    """Do not silently abandon settings belonging to a newly disabled agent."""
    _check_source_transition(source, files._load_manifest())


def _check_source_transition(source: SelectedManagedSource, manifest: dict) -> None:
    config = source.manifest
    if config is None:
        return
    enabled = config.get("enabled_agents", {})
    applications = manifest.get("applications", {})
    pending = manifest.get("pending", {})
    for agent in applications.keys() | pending.keys():
        application = applications.get(agent, {})
        if (
            agent != source.agent
            and agent not in enabled
            and (application.get("status") == "applied" or agent in pending)
        ):
            owner = pending.get(agent, {}).get("source", {}).get("owner") or application["owner"]
            raise RuntimeError(
                f"This source disables {agent}, which has applied or incomplete settings. Retry its application or run `ug managed-config release --owner {owner} --agent {agent}` before retrying this source."
            )


def apply_source(
    source: SelectedManagedSource,
    plans: list[DestinationPlan],
    *,
    process_env_keys: set[str] | None = None,
) -> dict[str, str]:
    """Preflight and commit one agent's entire application, retaining failed journals."""
    config = source.manifest or {}
    handoff = config.get("handoff") or {}
    owner = handoff.get("owner", "local-file") if source.kind == "file" else "workspace-api"
    return _transaction(source, plans, owner, handoff, process_env_keys=process_env_keys)


def release_owner(owner: str, agent: str) -> bool:
    """Remove this owner's effects without discovery, broad restoration, or agent launch."""
    if agent not in {"claude", "codex"} or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", owner
    ):
        raise RuntimeError("Release requires a valid owner and either claude or codex.")
    source = SelectedManagedSource(kind="file", workspace="", agent=agent)
    result = _transaction(source, [], owner, {}, release=True)
    return bool(result)


def revert_owned_destinations(agent: str) -> bool:
    """Restore tracked private/shared destinations for explicit broad ``ug revert``.

    OS-managed restoration continues through managed_files' privileged revert path.
    Ordinary apply and release never invoke this baseline restoration.
    """
    source = SelectedManagedSource(kind="file", workspace="", agent=agent)
    return bool(_transaction(source, [], "*", {}, release=True, restore=True))


def _transaction(
    source: SelectedManagedSource,
    plans: list[DestinationPlan],
    owner: str,
    handoff: dict,
    *,
    release: bool = False,
    restore: bool = False,
    process_env_keys: set[str] | None = None,
) -> dict[str, str]:
    agent = source.agent
    if files.MANAGED_BACKUP_DIR.is_symlink():
        raise RuntimeError("Refusing symlinked managed-backups directory.")
    with ExitStack() as stack:
        stack.enter_context(_lock(files.MANAGED_BACKUP_DIR / "manifest.lock"))
        manifest = files._load_manifest()
        if not release:
            _check_source_transition(source, manifest)
        manifest["version"] = 2
        entries = files._manifest_files(manifest)
        by_key = {files._destination_key(agent, plan.path, plan.target): plan for plan in plans}
        pending = manifest.get("pending", {}).get(agent, {})
        if release and not restore and pending and pending.get("source", {}).get("owner") != owner:
            raise RuntimeError(
                f"{agent} has another owner's incomplete application; retry that application before releasing {owner}."
            )
        effective = deepcopy(entries)
        effective.update(deepcopy(pending.get("recovery_entries", {})))
        legacy_generated = []
        for key, entry in effective.items():
            if (
                entry.get("agent") != agent
                or "active_effects" in entry
                or not entry.get("last_applied_file")
            ):
                continue
            plan = by_key.get(key) or _registered_plan(agent, entry["scope"], Path(entry["path"]))
            entry["active_effects"] = _legacy_effects(entry, plan.parser)
            entry["owner"] = "workspace-api"
            if entry["scope"] == "managed_settings":
                legacy_generated = entry["active_effects"]
        for key, plan in by_key.items():
            if (
                key not in effective
                and plan.target == "private_settings"
                and plan.baseline_supplied
            ):
                effective[key] = {
                    "agent": agent,
                    "scope": plan.target,
                    "path": str(plan.path),
                    "owner": "workspace-api",
                    "active_effects": deepcopy(legacy_generated),
                }
        for key in pending.get("attempted", []):
            proposed = pending.get("entries", {}).get(key)
            if proposed:
                previous = effective.setdefault(key, {})
                historical = list(previous.get("owned_paths", []))
                for path in proposed.get("owned_paths", []):
                    if path not in historical:
                        historical.append(path)
                combined = _merge_effects(
                    previous.get("active_effects", []), proposed.get("active_effects", [])
                )
                previous.update(deepcopy(proposed))
                previous["owned_paths"] = historical
                previous["active_effects"] = combined
                previous["owner"] = proposed.get("owner")
        if len(by_key) != len(plans) or len({plan.path for plan in plans}) != len(plans):
            raise RuntimeError("Duplicate destinations in the managed application.")
        for key, entry in effective.items():
            if entry.get("agent") != agent or not (entry.get("active_effects") or restore):
                continue
            if restore and entry.get("scope") in {"managed_settings", "requirements"}:
                continue
            if release and not restore and entry.get("owner") != owner:
                continue
            if key not in by_key:
                by_key[key] = _registered_plan(agent, entry["scope"], Path(entry["path"]))
        for path in sorted({plan.path for plan in by_key.values()}, key=str):
            stack.enter_context(
                _lock(
                    files.MANAGED_BACKUP_DIR
                    / "locks"
                    / (files._sha256(str(path.absolute())) + ".lock")
                )
            )
        declarations = handoff.get("agents", {}).get(agent, {})
        current_keys = (
            {
                env_name_identity(validate_env_name(name, "process_env"))
                for name in (process_env_keys or ())
            }
            if not release
            else set()
        )
        declaration_key = f"{owner}:{handoff.get('migration_version')}:{agent}"
        declaration_digest = files._sha256(_canonical(declarations))
        signatures = manifest.setdefault("handoff_declarations", {})
        if (
            handoff
            and declaration_key in signatures
            and signatures[declaration_key] != declaration_digest
        ):
            raise RuntimeError(
                f"Handoff declarations changed for {agent}; increment migration_version."
            )
        migrations: dict[str, tuple[str, str]] = {}
        receipts = manifest.setdefault("migration_receipts", {})
        env_receipt: tuple[str, str] | None = None
        retired_declarations: set[str] = set()
        for target in {
            item["target"]
            for action in ("adopt", "retire")
            for item in declarations.get(action, [])
        }:
            if target == "process_env":
                env_declarations = {
                    action: [
                        item for item in declarations.get(action, []) if item["target"] == target
                    ]
                    for action in ("adopt", "retire")
                }
                for items in env_declarations.values():
                    for item in items:
                        path = item.get("path")
                        if not isinstance(path, list) or len(path) != 1 or "elements" in item:
                            raise RuntimeError(
                                "process_env handoff requires one variable name and no elements."
                            )
                        validate_env_name(path[0], "handoff.process_env")
                receipt_key = f"{owner}:{handoff['migration_version']}:{agent}:process_env"
                digest = files._sha256(_canonical(env_declarations))
                if receipt_key in receipts and receipts[receipt_key] != digest:
                    raise RuntimeError(
                        f"Handoff declarations changed for {agent}.process_env; increment migration_version."
                    )
                if receipt_key not in receipts:
                    for item in env_declarations["adopt"]:
                        name = env_name_identity(item["path"][0])
                        if name not in current_keys:
                            raise RuntimeError(
                                f"Handoff adoption requires a current custom_env declaration for {name}."
                            )
                    retired_declarations = {
                        env_name_identity(item["path"][0]) for item in env_declarations["retire"]
                    }
                    env_receipt = (receipt_key, digest)
                continue
            matching = [(key, plan) for key, plan in by_key.items() if plan.target == target]
            if len(matching) != 1:
                raise RuntimeError(
                    f"Handoff target {target} is unavailable for this {agent} application."
                )
            key, _ = matching[0]
            digest = files._sha256(
                _canonical(
                    {
                        action: [
                            item
                            for item in declarations.get(action, [])
                            if item["target"] == target
                        ]
                        for action in ("adopt", "retire")
                    }
                )
            )
            receipt_key = f"{owner}:{handoff['migration_version']}:{key}"
            if receipt_key in receipts and receipts[receipt_key] != digest:
                raise RuntimeError(
                    f"Handoff declarations changed for {agent}.{target}; increment migration_version."
                )
            if receipt_key not in receipts:
                migrations[key] = (receipt_key, digest)
        prepared = []
        scopes = {}
        for key, plan in by_key.items():
            current_text, current = _read(plan)
            previous = effective.get(key, {})
            old_effects = (
                previous.get("active_effects", [])
                if not release or previous.get("owner") == owner
                else []
            )
            if (
                not release
                and key not in entries
                and key not in pending.get("entries", {})
                and not legacy_generated
            ):
                imported = deepcopy(plan.legacy_effects)
                for path in plan.legacy_owned_paths:
                    value = files._path_value(current, path)
                    if value is files._MISSING:
                        continue
                    for leaf in leaf_paths(value, path) if isinstance(value, dict) else [path]:
                        item = files._path_value(current, leaf)
                        if isinstance(item, list):
                            raise RuntimeError(
                                f"Legacy array migration needs explicit contributions at {plan.target}.{'.'.join(leaf)}."
                            )
                        imported.append({"path": leaf, "value": files._unwrap(item)})
                if imported:
                    old_effects = _merge_effects(old_effects, imported)
                    previous = {
                        **previous,
                        "agent": agent,
                        "scope": plan.target,
                        "path": str(plan.path),
                        "owner": "workspace-api",
                        "active_effects": old_effects,
                        "migration_provenance": "previous-applied-state",
                    }
                    effective[key] = previous
                    print_warning(
                        f"Adopting previously applied fields in {plan.target}: {', '.join('.'.join(effect['path']) for effect in imported)}"
                    )
            if plan.generated_artifact and plan.target not in {
                "model_catalog",
                "scoped_model_catalog",
            }:
                raise RuntimeError(
                    "Only internal catalog artifacts support complete document ownership."
                )
            base = {} if plan.generated_artifact and not release else deepcopy(current)
            drifted = [
                ".".join(effect["path"])
                for effect in old_effects
                if _effect_drifted(current, effect)
            ]
            if drifted:
                description = ", ".join(drifted[:20])
                if len(drifted) > 20:
                    description += f" and {len(drifted) - 20} more fields"
                print_warning(f"Reconciling edited owned fields in {plan.target}: {description}")
            _remove_effects(base, old_effects)
            if key in migrations:
                for item in declarations.get("retire", []):
                    if item["target"] == plan.target:
                        value = files._path_value(base, item["path"])
                        if (
                            isinstance(value, dict)
                            or isinstance(value, list)
                            and "elements" not in item
                        ):
                            raise RuntimeError(
                                f"Retirement must name leaf fields or explicit array elements at {plan.target}.{'.'.join(item['path'])}."
                            )
                        _remove_effects(base, [item])
            desired = plan.compose(deepcopy(base)) if not release else base
            restored_text = files._MISSING
            if restore:
                original_text = files._original_text(previous)
                last_text = files._snapshot_text(previous, "last_applied_file")
                if current_text is None or last_text is None:
                    restored_text, desired = current_text, current
                else:
                    desired = files._three_way_revert(
                        current,
                        plan.parser(original_text) if original_text is not None else {},
                        plan.parser(last_text),
                        cast(list, previous.get("owned_paths", [])),
                    )
                    if not desired and original_text is None:
                        restored_text = None
            effects = _effects(plan, base, desired) if not release else []
            if key in migrations:
                for item in declarations.get("adopt", []):
                    if item["target"] != plan.target:
                        continue
                    adopted = next(
                        (effect for effect in effects if effect["path"] == item["path"]), None
                    )
                    if adopted is None:
                        raise RuntimeError(
                            f"Handoff adoption requires a current declaration at {plan.target}.{'.'.join(item['path'])}; use retire for omitted fields."
                        )
                    if "elements" in adopted and "elements" not in item:
                        raise RuntimeError(
                            f"Array adoption requires explicit elements at {plan.target}.{'.'.join(item['path'])}."
                        )
                    if "elements" in item and _canonical(item["elements"]) != _canonical(
                        adopted.get("elements")
                    ):
                        raise RuntimeError(
                            f"Adoption contributions differ from the current declaration at {plan.target}.{'.'.join(item['path'])}."
                        )
            # Recompose contribution arrays from the cleaned live remainder and the
            # declared source elements, never claim user elements merged by adapters.
            for path in plan.exact_array_paths if not release else ():
                remainder = files._path_value(base, list(path))
                if not isinstance(remainder, list):
                    continue
                adopted_elements = []
                if key in migrations:
                    for item in declarations.get("adopt", []):
                        if item["target"] == plan.target and tuple(item["path"]) == path:
                            adopted_elements = item.get("elements", [])
                if _remove_elements(remainder, adopted_elements):
                    raise RuntimeError(
                        f"Unowned array elements at {plan.target}.{'.'.join(path)}; "
                        "declare explicit adopt or retire elements and increment migration_version."
                    )
            for effect in effects:
                if "elements" in effect and tuple(effect["path"]) not in plan.exact_array_paths:
                    remainder = files._path_value(base, effect["path"])
                    prior = next(
                        (
                            old
                            for old in old_effects
                            if old.get("path") == effect["path"] and "elements" in old
                        ),
                        None,
                    )
                    unchanged_contribution = prior is not None and _canonical(
                        prior["elements"]
                    ) == _canonical(effect["elements"])
                    if unchanged_contribution:
                        remainder = files._path_value(current, effect["path"])
                    remainder = remainder if isinstance(remainder, list) else []
                    additions = effect["elements"]
                    if prior is None or unchanged_contribution:
                        additions = _remove_elements(additions, remainder)
                    combined = _add_elements(remainder, additions)
                    files._set_path_value(desired, effect["path"], combined)
            unchanged = files.is_semantically_equal(current, desired)
            desired_text = current_text if unchanged else plan.dumper(desired)
            if restored_text is not files._MISSING:
                desired_text = cast(str | None, restored_text)
            if not desired and plan.delete_when_empty:
                desired_text = None
            scope = "managed" if plan.privileged else "applied"
            compatible = plan.compatible is not None and plan.compatible(current, desired)
            needs_cleanup = bool(old_effects or key in migrations)
            if (
                plan.privileged
                and (not plan.writable or not files.managed_writes_allowed())
                and desired_text != current_text
            ):
                if plan.optional and compatible and not needs_cleanup:
                    desired_text, desired, effects, scope = (
                        current_text,
                        current,
                        [],
                        plan.compatible_scope,
                    )
                else:
                    raise RuntimeError(
                        f"Cannot update {agent} {plan.target} at {plan.path} non-interactively; run from an interactive terminal or release the previous owner."
                    )
            elif not plan.writable:
                if not compatible:
                    raise RuntimeError(
                        f"Existing {agent} {plan.target} blocks this launch; reconcile or release its settings first."
                    )
                scope = plan.compatible_scope
            if plan.privileged and desired_text != current_text:
                files._validate_sudo_replace_target(plan.path)
            plan.parser(desired_text) if desired_text is not None else None
            prepared.append((key, plan, current_text, desired_text, effects, scope))
            scopes[plan.target] = scope
        if is_dry_run():
            return scopes
        proposed_entries = {}
        for key, plan, current_text, _desired_text, effects, scope in prepared:
            entry = deepcopy(_backup(manifest, plan, agent, current_text))
            historical = entry.setdefault("owned_paths", [])
            # Failed applications can own fields absent from both the committed entry
            # and this retry. Broad revert still needs their original baseline paths.
            for path in [
                *effective.get(key, {}).get("owned_paths", []),
                *(effect["path"] for effect in effects),
            ]:
                if path not in historical:
                    historical.append(path)
            entry.update(owner=owner, active_effects=effects, applied_scope=scope)
            entry["source"] = {
                "kind": source.kind,
                "workspace": source.workspace,
                "path": str(source.resolved_path) if source.resolved_path else None,
                "digest": source.digest,
            }
            entry["rendered_digest"] = files._sha256(_canonical(effects))
            provenance = effective.get(key, {}).get("migration_provenance")
            if provenance:
                entry["migration_provenance"] = provenance
            proposed_entries[key] = entry
        source_record = _source_record(source, owner)
        previous_env = {
            env_name_identity(name): value
            for name, value in manifest.setdefault("process_env", {}).get(agent, {}).items()
        }
        previous_env.update(
            {
                env_name_identity(name): value
                for name, value in pending.get("process_env", {}).items()
            }
        )
        journal = {
            "source": source_record,
            "operation": "release" if release else "apply",
            "entries": proposed_entries,
            "recovery_entries": {
                key: deepcopy(entry)
                for key, entry in effective.items()
                if entry.get("agent") == agent and entry.get("active_effects")
            },
            "attempted": [],
            "process_env": {**previous_env, **dict.fromkeys(current_keys, owner)},
            "before": {key: text for key, _, text, _, _, _ in prepared},
        }
        manifest.setdefault("pending", {})[agent] = journal
        files._write_manifest(manifest)
        verified_texts = {}
        for key, plan, current_text, desired_text, _effects_list, _scope in prepared:
            if files.read_managed_file(plan.path) != current_text:
                raise RuntimeError(
                    f"Concurrent edit at {plan.path}; preserved the newer file. Retry."
                )
            journal["attempted"].append(key)
            files._write_manifest(manifest)
            if desired_text != current_text:
                if plan.privileged:
                    files._print_managed_write_permission(agent)
                    try:
                        # Managed files remain regular documents even after cleanup.
                        files._sudo_replace(
                            plan.path, desired_text or plan.dumper({}), expected_text=current_text
                        )
                    except (PermissionError, files.subprocess.CalledProcessError) as exc:
                        if (
                            plan.optional
                            and not effective.get(key, {}).get("active_effects")
                            and key not in migrations
                            and plan.compatible is not None
                            and plan.compatible(
                                plan.parser(current_text) if current_text else {},
                                plan.parser(desired_text) if desired_text else {},
                            )
                        ):
                            print_warning(
                                f"{agent}: managed settings unavailable; continuing with local settings."
                            )
                            desired_text = current_text
                            proposed_entries[key]["active_effects"] = []
                            proposed_entries[key]["rendered_digest"] = files._sha256(_canonical([]))
                            proposed_entries[key]["applied_scope"] = plan.compatible_scope
                            scopes[plan.target] = plan.compatible_scope
                        else:
                            raise RuntimeError(
                                f"Could not apply {agent} {plan.target}; recovery journal retained. Retry from an interactive terminal."
                            ) from exc
                else:
                    _atomic_replace(plan.path, desired_text, current_text)
            if files.read_managed_file(plan.path) != desired_text:
                raise RuntimeError(
                    f"Could not verify {agent} {plan.target}; recovery journal retained. Retry."
                )
            verified_texts[plan.path] = desired_text
            entry = proposed_entries[key]
            if desired_text is not None:
                filename = (
                    f"{key}.last-applied-{files._sha256(desired_text)[:16]}{plan.path.suffix}"
                )
                files._write_private_file(files.MANAGED_BACKUP_DIR / filename, desired_text)
                entry.update(
                    last_applied_file=filename, last_applied_sha256=files._sha256(desired_text)
                )
            if key in migrations:
                receipt_key, digest = migrations[key]
                receipts[receipt_key] = digest
                signatures[declaration_key] = declaration_digest
            files._write_manifest(manifest)
        for path, expected in verified_texts.items():
            if files.read_managed_file(path) != expected:
                raise RuntimeError(
                    f"Concurrent edit at {path}; application remains incomplete. Retry."
                )
        entries.update(proposed_entries)
        if handoff:
            signatures[declaration_key] = declaration_digest
        retired = {
            env_name_identity(name): value
            for name, value in manifest.setdefault("retired_env", {}).get(agent, {}).items()
        }
        manifest["retired_env"][agent] = retired
        if (
            current_keys
            or retired_declarations
            or any(
                restore or previous_owner == owner
                for previous_owner in [*previous_env.values(), *retired.values()]
            )
        ):
            scopes["process_env"] = "released" if release else "applied"
        for key, previous_owner in previous_env.items():
            if not release and key not in current_keys:
                retired[key] = previous_owner
        if release:
            retired = {
                key: value for key, value in retired.items() if not restore and value != owner
            }
            previous_env = {
                key: value for key, value in previous_env.items() if not restore and value != owner
            }
            manifest["retired_env"][agent] = retired
            manifest["process_env"][agent] = previous_env
            application = manifest.setdefault("applications", {}).get(agent)
            if application and (restore or application.get("owner") == owner):
                application["status"] = "released"
            manifest.setdefault("releases", {})[f"{agent}:{owner}"] = "released"
        else:
            for key in retired_declarations:
                retired[key] = owner
            for key in current_keys:
                retired.pop(key, None)
            manifest["process_env"][agent] = dict.fromkeys(current_keys, owner)
            if env_receipt is not None:
                receipt_key, digest = env_receipt
                receipts[receipt_key] = digest
            source_record["rendered_digest"] = files._sha256(
                _canonical(
                    {
                        "destinations": {
                            key: entry["active_effects"] for key, entry in proposed_entries.items()
                        },
                        "process_env_keys": sorted(current_keys),
                    }
                )
            )
            manifest.setdefault("applications", {})[agent] = source_record
        manifest["pending"].pop(agent, None)
        if restore:
            for key in proposed_entries:
                entries.pop(key, None)
        files._write_manifest(manifest)
        return scopes
