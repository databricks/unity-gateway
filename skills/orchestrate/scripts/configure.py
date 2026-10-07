#!/usr/bin/env python3
"""Resolve shared model preferences and manage owned Claude agent definitions."""

import argparse
import hashlib
import json
import os
import re
import tempfile
import tomllib
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ucode.codex_config import (
    DEFAULT_CODEX_CONFIG_PATH,
    codex_config_precedence_paths,
    codex_managed_config_path,
)
from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock, release_file_lock
from ucode.smart_routing.orchestrator import require_enabled

PACKAGE = Path(__file__).resolve().parents[1]
ROLES = ("explorer", "researcher", "worker", "tester", "reviewer")
EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
}
_CODEX_GATEWAY_PREFIX = "system.ai."
_CODEX_NATIVE_GPT_VERSION = re.compile(r"^(gpt-\d+)\.(\d+)(?=-|$)")
_CODEX_GATEWAY_GPT_VERSION = re.compile(r"^(gpt-\d+)-(\d+)(?=-|$)")


def check_path(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Refusing symlink: {part}")
    if path.exists() and not path.is_file():
        raise ValueError(f"Not a regular file: {path}")


def validate_choice(harness, role, choice):
    if not isinstance(choice, dict) or set(choice) - {"model", "effort"}:
        raise ValueError(f"Invalid choice for {harness}/{role}")
    model = choice.get("model")
    if (
        not isinstance(model, str)
        or not model
        or any(character.isspace() or not character.isprintable() for character in model)
    ):
        raise ValueError(f"Invalid model for {harness}/{role}")
    if choice.get("effort") is not None and choice["effort"] not in EFFORTS[harness]:
        raise ValueError(f"Invalid effort for {harness}/{role}: expected {EFFORTS[harness]}")


def codex_model_candidates(model):
    if "/" in model or ":" in model:
        return [model]
    if model.startswith(_CODEX_GATEWAY_PREFIX):
        gateway_slug = model.removeprefix(_CODEX_GATEWAY_PREFIX)
        native = _CODEX_GATEWAY_GPT_VERSION.sub(r"\1.\2", gateway_slug, count=1)
        return [model, native] if native else [model]
    gateway_slug = _CODEX_NATIVE_GPT_VERSION.sub(r"\1-\2", model, count=1)
    return [model, _CODEX_GATEWAY_PREFIX + gateway_slug]


def active_codex_catalog_path():
    for config_path in codex_config_precedence_paths(
        codex_managed_config_path(), DEFAULT_CODEX_CONFIG_PATH
    ):
        if not config_path.exists():
            continue
        try:
            with config_path.open("rb") as stream:
                configured = tomllib.load(stream).get("model_catalog_json")
        except (OSError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid Codex configuration: {config_path}") from error
        if configured is None:
            continue
        if not isinstance(configured, str) or not configured:
            raise ValueError(f"Invalid model_catalog_json in {config_path}")
        path = Path(configured).expanduser()
        return (path if path.is_absolute() else config_path.parent / path).resolve()
    return None


def read_codex_catalog_slugs(path):
    try:
        catalog = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(f"Invalid Codex model catalog: {path}") from error
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if (
        not isinstance(models, list)
        or not models
        or any(
            not isinstance(model, dict) or not isinstance(model.get("slug"), str)
            for model in models
        )
    ):
        raise ValueError(f"Invalid Codex model catalog: {path}")
    return {model["slug"] for model in models}


def read_config(path):
    check_path(path)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(
            f"Invalid JSON configuration: {path}; repair or restore it before retrying"
        ) from error
    if not isinstance(data, dict) or set(data) - {"claude", "codex", "_generated"}:
        raise ValueError(f"Invalid configuration keys: {path}")
    return data


def validate_ownership(data, path):
    receipts = data.get("_generated", {})
    if not isinstance(receipts, dict) or set(receipts) - set(ROLES):
        raise ValueError(f"Invalid ownership record: {path}")
    if any(
        not isinstance(receipt, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt)
        for receipt in receipts.values()
    ):
        raise ValueError(f"Invalid ownership hash: {path}")


def load_config(path, *, validate_choices=True, harness=None):
    data = read_config(path)
    for selected_harness in (harness,) if harness is not None else ("claude", "codex"):
        roles = data.get(selected_harness, {})
        if not isinstance(roles, dict) or set(roles) - set(ROLES):
            raise ValueError(f"Invalid {selected_harness} roles: {path}")
        if validate_choices:
            for role, choice in roles.items():
                validate_choice(selected_harness, role, choice)
    if harness != "codex":
        validate_ownership(data, path)
    return data


def scope_paths(project):
    if project is not None:
        project = project.expanduser().resolve()
        if not project.is_dir():
            raise ValueError(f"Project directory does not exist: {project}")
        return project / ".model-orchestrator.json", project / ".claude" / "agents"
    config_root = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser().resolve()
    )
    claude_root = (
        Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        .expanduser()
        .resolve()
    )
    return config_root / "model-orchestrator" / "config.json", claude_root / "agents"


def agent_name(role, project):
    return f"model-orchestrator-custom-{'project' if project is not None else 'user'}-{role}"


def agent_content(role, choice, project):
    text = (PACKAGE / "agents" / f"{role}.md").read_text()
    text = re.sub(r"^name: .+$", f"name: {agent_name(role, project)}", text, count=1, flags=re.M)
    model = "model: " + json.dumps(choice["model"])
    if choice.get("effort") is not None:
        model += "\neffort: " + choice["effort"]
    return re.sub(r"^model: .+$", lambda _: model, text, count=1, flags=re.M).encode()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def replace_file(path, content):
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".model-orchestrator-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def configuration_lock(path, *, read_only_lock_file=False):
    check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    if lock.is_dir() and not lock.is_symlink():
        raise ValueError(f"Legacy lock directory: {lock}; remove only after its writer exits")
    check_path(lock)
    mode = "rb" if read_only_lock_file and lock.exists() else "a+b"
    with lock.open(mode) as stream:
        acquire_exclusive_file_lock(stream)
        try:
            yield
        finally:
            release_file_lock(stream)


def transaction_paths(project):
    config_path, agent_dir = scope_paths(project)
    journal = config_path.with_name(config_path.name + ".transaction.json")
    paths = {"config": config_path}
    paths.update({role: agent_dir / f"{agent_name(role, project)}.md" for role in ROLES})
    return journal, paths


def recovery_entry(before, after):
    return {
        "before": before.hex() if before is not None else None,
        "after": after.hex() if after is not None else None,
    }


def recover_scope(project):
    journal, paths = transaction_paths(project)
    check_path(journal)
    if not journal.exists():
        return
    entries = json.loads(journal.read_text())
    if not isinstance(entries, dict) or "config" not in entries or set(entries) - set(paths):
        raise ValueError(f"Invalid recovery journal: {journal}")
    restores = []
    for name, entry in entries.items():
        if not isinstance(entry, dict) or set(entry) != {"before", "after"}:
            raise ValueError(f"Invalid recovery entry: {journal}")
        if any(value is not None and not isinstance(value, str) for value in entry.values()):
            raise ValueError(f"Invalid recovery content: {journal}")
        before = bytes.fromhex(entry["before"]) if entry["before"] is not None else None
        after = bytes.fromhex(entry["after"]) if entry["after"] is not None else None
        target = paths[name]
        check_path(target)
        existing = target.read_bytes() if target.exists() else None
        if existing not in (before, after):
            raise ValueError(f"Preserving edited file during recovery: {target}")
        if existing != before:
            restores.append((target, before))
    for target, content in reversed(restores):
        replace_file(target, content)
    journal.unlink()


def update_scope(project, previous, desired, *, manage_claude=True):
    config_path, agent_dir = scope_paths(project)
    changes = {}
    receipts = {}
    for role in ROLES if manage_claude else ():
        path = agent_dir / f"{agent_name(role, project)}.md"
        owned = previous.get("_generated", {}).get(role)
        choice = desired.get("claude", {}).get(role)
        if not owned and choice is None:
            continue
        check_path(path)
        existing = path.read_bytes() if path.exists() else None
        if existing is not None and (not owned or digest(existing) != owned):
            raise ValueError(f"Preserving unowned or edited agent: {path}")
        content = agent_content(role, choice, project) if choice else None
        if content is not None:
            receipts[role] = digest(content)
        if existing != content:
            changes[path] = content
    if manage_claude:
        desired = {key: value for key, value in desired.items() if key != "_generated"}
        if receipts:
            desired["_generated"] = receipts
    check_path(config_path)
    changes[config_path] = (json.dumps(desired, indent=2) + "\n").encode() if desired else None
    before = {path: path.read_bytes() if path.exists() else None for path in changes}
    changes = {path: content for path, content in changes.items() if before[path] != content}
    if not changes:
        return []
    journal, paths = transaction_paths(project)
    check_path(journal)
    if journal.exists():
        raise ValueError(f"Pending recovery journal: {journal}; run show before updating")
    entries = {
        name: recovery_entry(before[path], changes[path])
        for name, path in paths.items()
        if path in changes
    }
    if "config" not in entries:
        entries["config"] = recovery_entry(before[config_path], before[config_path])
    replace_file(journal, (json.dumps(entries) + "\n").encode())
    try:
        for path, content in changes.items():
            replace_file(path, content)
        journal.unlink()
    except OSError:
        recover_scope(project)
        raise
    return [str(path) for path in changes]


def resolve(harness, project):
    require_enabled()
    with ExitStack() as locks:
        scopes = []
        for scope in [None, project] if project is not None else [None]:
            config_path = scope_paths(scope)[0]
            journal = transaction_paths(scope)[0]
            check_path(journal)
            if config_path.exists() or journal.exists():
                locks.enter_context(configuration_lock(config_path, read_only_lock_file=True))
                recover_scope(scope)
            scopes.append(
                (scope, load_config(config_path, validate_choices=False, harness=harness))
            )
        catalog_path = active_codex_catalog_path() if harness == "codex" else None
        return resolve_models(harness, scopes, catalog_path)


def resolve_models(harness, scopes, catalog_path=None):
    catalog_slugs = read_codex_catalog_slugs(catalog_path) if catalog_path is not None else None
    result = {}
    for role in ROLES:
        choice = (
            {"model": "gpt-5.6-luna", "effort": "max"}
            if harness == "codex"
            else {"model": "sonnet", "effort": None}
        )
        configured = False
        subagent_type = f"ug-smart-router:{role}"
        for scope, data in reversed(scopes):
            if role not in data.get(harness, {}):
                continue
            validate_choice(harness, role, data[harness][role])
            choice = {"effort": None, **data[harness][role]}
            configured = True
            if harness == "claude":
                subagent_type = agent_name(role, scope)
                path = scope_paths(scope)[1] / f"{subagent_type}.md"
                check_path(path)
                expected = agent_content(role, choice, scope)
                if (
                    not path.exists()
                    or path.read_bytes() != expected
                    or data.get("_generated", {}).get(role) != digest(expected)
                ):
                    raise ValueError(
                        f"Missing/stale Claude agent; run set for {role} again: {path}"
                    )
            break
        result[role] = dict(choice)
        if harness == "claude":
            result[role]["subagent_type"] = subagent_type
        else:
            result[role]["reasoning_effort"] = result[role].pop("effort")
            result[role]["allow_inherited_fallback"] = not configured
            candidates = codex_model_candidates(result[role]["model"])
            if catalog_slugs is not None and len(candidates) > 1:
                # A missing catalog entry is not an invalid preference. Preserve
                # the fallback policy while native spawn establishes availability.
                result[role]["model"] = next(
                    (candidate for candidate in candidates if candidate in catalog_slugs),
                    result[role]["model"],
                )
    if harness == "claude" and os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "").lower() in (
        "1",
        "true",
    ):
        forced = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")
        if not forced or forced == "inherit":
            raise ValueError(
                "CLAUDE_CODE_SUBAGENT_MODEL_FORCE selects the parent model; role models cannot be resolved"
            )
        if any(choice["model"] != forced for choice in result.values()):
            raise ValueError(
                "CLAUDE_CODE_SUBAGENT_MODEL_FORCE conflicts with the configured role models"
            )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("show", "set", "unconfigure"))
    parser.add_argument("--harness", choices=tuple(EFFORTS))
    parser.add_argument("--role", choices=ROLES)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", type=Path)
    scope.add_argument("--user", action="store_true")
    args = parser.parse_args()
    if args.action == "unconfigure" and args.harness:
        parser.error("unconfigure removes the selected scope; omit --harness")
    if args.action != "unconfigure" and not args.harness:
        parser.error("show/set requires --harness")
    if args.action == "set" and (not args.role or not args.model):
        parser.error("set requires --role and --model")
    if args.action != "set" and any((args.role, args.model, args.effort)):
        parser.error("--role, --model, and --effort require set")
    result: dict
    try:
        if args.action == "show":
            result = resolve(args.harness, args.project)
        else:
            path = scope_paths(args.project)[0]
            with configuration_lock(path):
                recover_scope(args.project)
                if args.action == "unconfigure":
                    previous = read_config(path)
                    validate_ownership(previous, path)
                else:
                    previous = load_config(
                        path, validate_choices=args.harness != "codex", harness=args.harness
                    )
                desired: dict = json.loads(json.dumps(previous)) if args.action == "set" else {}
                if args.action == "set":
                    desired.setdefault(args.harness, {})[args.role] = {
                        "model": args.model,
                        "effort": args.effort,
                    }
                    validate_choice(args.harness, args.role, desired[args.harness][args.role])
                result = {
                    "changed": update_scope(
                        args.project, previous, desired, manage_claude=args.harness != "codex"
                    )
                }
            if args.harness == "claude" and result["changed"]:
                result["next"] = (
                    "Restart Claude Code after initial setup; run show to verify the resolved map."
                )
        print(json.dumps(result, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
