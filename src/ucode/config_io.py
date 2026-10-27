"""File I/O, dry-run flag, backup/restore, deep-merge, dotenv parsing."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TypedDict, cast

import tomlkit
import tomlkit.exceptions

from ucode.ui import console


class ToolSpec(TypedDict):
    binary: str
    package: str
    display: str
    config_path: Path
    backup_path: Path


APP_DIR = Path.home() / ".ucode"

_dry_run = False


def set_dry_run(value: bool) -> None:
    global _dry_run
    _dry_run = bool(value)


def is_dry_run() -> bool:
    return _dry_run


def ensure_parent_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Failed to create directory for {path}") from exc


def atomic_write_json(path: Path, payload: object) -> None:
    """Write ``payload`` as indented JSON to ``path`` atomically (temp file + ``os.replace``).

    The temp file is created in the destination directory and renamed into place, so a crash, a
    full disk, or a concurrent reader never sees a half-written file. This is a raw writer: unlike
    ``write_json_file`` it does not honor dry-run, so callers that should be skipped under dry-run
    must guard the call themselves.
    """
    ensure_parent_dir(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
        os.replace(tmp_name, path)
    except OSError as exc:
        Path(tmp_name).unlink(missing_ok=True)
        raise RuntimeError(f"Failed to write file: {path}") from exc


def backup_existing_file(config_path: Path, backup_path: Path) -> bool:
    if _dry_run:
        return False
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if backup_path.exists():
            return True
        if not config_path.exists():
            return False
        backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except OSError as exc:
        raise RuntimeError(f"Failed to back up config from {config_path}") from exc


def restore_file(config_path: Path, backup_path: Path, managed: bool) -> bool:
    try:
        if backup_path.exists():
            ensure_parent_dir(config_path)
            config_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
            backup_path.unlink()
            return True
        if managed and config_path.exists():
            config_path.unlink()
            return True
        return False
    except OSError as exc:
        raise RuntimeError(f"Failed to restore config at {config_path}") from exc


def write_text_file(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    if _dry_run:
        console.print(f"\n[bold]\\[dry run] {path}[/bold]\n{content}")
        return
    ensure_parent_dir(path)
    try:
        path.write_text(content, encoding=encoding)
    except OSError as exc:
        raise RuntimeError(f"Failed to write config file: {path}") from exc


def write_json_file(path: Path, payload: dict) -> None:
    content = json.dumps(payload, indent=2) + "\n"
    if _dry_run:
        console.print(f"\n[bold]\\[dry run] {path}[/bold]\n{content}")
        return
    ensure_parent_dir(path)
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write config file: {path}") from exc


def deep_merge_dict(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base; overlay wins for conflicting leaves.

    Mutates and returns base. Nested dicts are merged; everything else is replaced.
    """
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            deep_merge_dict(base[key], val)
        else:
            base[key] = val
    return base


def prune_key_paths(doc: dict, key_paths: list[list[str]]) -> bool:
    """Surgically remove each ``key_path`` from a nested dict, dropping emptied parents.

    ``key_paths`` is a list of paths like ``[["env", "ANTHROPIC_BASE_URL"], ["apiKeyHelper"]]``.
    Only the exact leaves are removed; sibling keys the user set themselves stay untouched, and a
    parent dict left empty by the removal is dropped too. Returns True if anything changed.

    Used to undo ucode's writes to an agent's *native* config file (which it shares with the
    user's own settings), where restoring a backup would clobber edits made since ucode first ran.
    """
    changed = False
    for path in key_paths:
        if _prune_one(doc, list(path)):
            changed = True
    return changed


def _prune_one(node: object, path: list[str]) -> bool:
    if not path or not isinstance(node, dict):
        return False
    mapping = cast("dict[str, object]", node)
    key = path[0]
    if key not in mapping:
        return False
    if len(path) == 1:
        mapping.pop(key, None)
        return True
    child = mapping.get(key)
    removed = _prune_one(child, path[1:])
    if removed and isinstance(child, dict) and not child:
        mapping.pop(key, None)
    return removed


def read_json_safe(path: Path) -> dict:
    # `path.exists()` is inside the try: stat-ing a file under a root-locked dir (e.g. a
    # root-owned /etc/codex) raises PermissionError, which must read as "absent/unreadable → {}"
    # rather than crash a launch that only wanted to merge into it.
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def apply_json_mcp_diff(
    path: Path,
    key: str,
    add: dict[str, dict],
    remove: set[str],
    *,
    backup_path: Path | None = None,
) -> set[str]:
    """Apply an ``add``/``remove`` MCP-server diff to the ``key`` map in a JSON config, in one
    read-modify-write. Shared by the agents whose MCP servers live in a JSON object keyed by name
    (copilot/cursor/gemini/opencode); the developer's own entries and every other key are kept.
    ``backup_path`` snapshots the file first when the agent keeps a revert backup. Returns the subset
    of ``remove`` names that were actually present (so callers can report only real removals)."""
    if backup_path is not None:
        backup_existing_file(path, backup_path)
    existing = read_json_safe(path)
    servers = existing.get(key)
    if not isinstance(servers, dict):
        servers = {}
    removed = {name for name in remove if name in servers}
    for name in remove:
        servers.pop(name, None)
    servers.update(add)
    existing[key] = servers
    write_json_file(path, existing)
    return removed


def read_toml_safe(path: Path) -> tomlkit.TOMLDocument:
    # See read_json_safe: keep `path.exists()` inside the try so a PermissionError on a locked
    # parent directory is treated as an empty document rather than propagating.
    try:
        if not path.exists():
            return tomlkit.document()
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, tomlkit.exceptions.TOMLKitError):
        return tomlkit.document()


def write_toml_file(path: Path, doc: tomlkit.TOMLDocument) -> None:
    content = tomlkit.dumps(doc)
    if _dry_run:
        console.print(f"\n[bold]\\[dry run] {path}[/bold]\n{content}")
        return
    ensure_parent_dir(path)
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write config file: {path}") from exc


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE / KEY="VALUE" .env file, preserving insertion order.

    Comments and blank lines are dropped on round-trip. Lines that don't look
    like KEY=... are skipped. Leading whitespace (line indentation and spacing
    after the ``=``) is trimmed, but the exact characters up to the end of the
    line are preserved — including any trailing spaces — so a value such as
    ``token = abc123 `` keeps its trailing space.
    """
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for raw_line in text.splitlines():
        # Detect blank / comment lines on the fully-stripped form so trailing
        # spaces on value lines don't change which lines are skipped.
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        # Only strip *leading* whitespace from the line so the characters
        # between "=" and end-of-line (including trailing spaces) are preserved.
        key, _, val = raw_line.lstrip().partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.lstrip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        env[key] = val
    return env


def write_dotenv(path: Path, env: dict[str, str]) -> None:
    content = "".join(f'{key}="{val}"\n' for key, val in env.items())
    write_text_file(path, content)


# --- JSONC (JSON with comments and trailing commas, e.g. VS Code's settings.json) ---------------

_JSONC_PUNCTUATION = frozenset("{}[]:,")


def _jsonc_tokens(text: str) -> list[tuple[str, int, int]]:
    """``(token, start, end)`` for each token in ``text``, skipping whitespace and comments.

    Raises ValueError on an unterminated string or comment.
    """
    tokens: list[tuple[str, int, int]] = []
    i, length = 0, len(text)
    while i < length:
        char = text[i]
        if char.isspace():
            i += 1
        elif text.startswith("//", i):
            newline = text.find("\n", i)
            i = length if newline < 0 else newline + 1
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            if close < 0:
                raise ValueError("unterminated comment")
            i = close + 2
        elif char == '"':
            end = i + 1
            while end < length and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            if end >= length:
                raise ValueError("unterminated string")
            tokens.append((text[i : end + 1], i, end + 1))
            i = end + 1
        elif char in _JSONC_PUNCTUATION:
            tokens.append((char, i, i + 1))
            i += 1
        else:
            end = i
            while end < length and not text[end].isspace() and text[end] not in _JSONC_PUNCTUATION:
                if text.startswith(("//", "/*"), end):
                    break
                end += 1
            tokens.append((text[i:end], i, end))
            i = end
    return tokens


def loads_jsonc(text: str) -> object:
    """Parse JSONC: comments and trailing commas are dropped, then it's read as JSON."""
    tokens = _jsonc_tokens(text)
    kept = [
        token
        for index, (token, _, _) in enumerate(tokens)
        if not (token == "," and index + 1 < len(tokens) and tokens[index + 1][0] in "}]")
    ]
    return json.loads(" ".join(kept)) if kept else None


class _JsoncEntry(TypedDict):
    key: str
    key_start: int
    value_start: int
    value_end: int
    comma_end: int | None


def _jsonc_top_level(text: str) -> tuple[int, int, list[_JsoncEntry]]:
    """The top-level object's ``{`` and ``}`` offsets and its entries; ValueError if it isn't one."""
    tokens = _jsonc_tokens(text)
    if not tokens or tokens[0][0] != "{":
        raise ValueError("the top level isn't an object")
    entries: list[_JsoncEntry] = []
    index = 1
    while index < len(tokens) and tokens[index][0] != "}":
        key, key_start, _ = tokens[index]
        if not key.startswith('"') or index + 2 >= len(tokens) or tokens[index + 1][0] != ":":
            raise ValueError("expected a key and a value")
        index += 2
        value_start = tokens[index][1]
        depth = 0
        while True:
            token = tokens[index][0]
            depth += 1 if token in "{[" else -1 if token in "}]" else 0
            if depth <= 0:
                break
            index += 1
            if index >= len(tokens):
                raise ValueError("unterminated value")
        value_end = tokens[index][2]
        index += 1
        comma_end = None
        if index < len(tokens) and tokens[index][0] == ",":
            comma_end = tokens[index][2]
            index += 1
        entries.append(
            {
                "key": json.loads(key),
                "key_start": key_start,
                "value_start": value_start,
                "value_end": value_end,
                "comma_end": comma_end,
            }
        )
    if index >= len(tokens) or index != len(tokens) - 1:
        raise ValueError("unexpected content after the top-level object")
    return tokens[0][1], tokens[index][1], entries


def _line_start(text: str, offset: int) -> int:
    """The start of ``offset``'s line when only whitespace precedes it there, else ``offset``."""
    start = text.rfind("\n", 0, offset) + 1
    return start if not text[start:offset].strip() else offset


def _line_end(text: str, offset: int) -> int:
    newline = text.find("\n", offset)
    return len(text) if newline < 0 else newline


def _remove_jsonc_entry(text: str, entries: list[_JsoncEntry], position: int) -> str:
    entry = entries[position]
    start = _line_start(text, entry["key_start"])
    end = entry["comma_end"] if entry["comma_end"] is not None else entry["value_end"]
    rest = text[end : _line_end(text, end)].strip()
    # A comment after the entry on its own line goes with it.
    if start != entry["key_start"] and (not rest or rest.startswith("//")):
        end = min(_line_end(text, end) + 1, len(text))
    text = text[:start] + text[end:]
    previous_comma = entries[position - 1]["comma_end"] if position > 0 else None
    if entry["comma_end"] is None and previous_comma is not None:
        text = text[: previous_comma - 1] + text[previous_comma:]
    return text


def update_jsonc_keys(text: str, values: dict[str, object], remove: tuple[str, ...] = ()) -> str:
    """Set ``values`` and delete ``remove`` among the top-level keys of a JSONC object, leaving
    every other byte (comments included) as it was. Raises ValueError if ``text`` isn't one."""
    if not _jsonc_tokens(text):
        text = text + ("\n" if text.strip() else "") + "{}"
    for key in remove:
        _, _, entries = _jsonc_top_level(text)
        position = next((i for i, entry in enumerate(entries) if entry["key"] == key), None)
        if position is not None:
            text = _remove_jsonc_entry(text, entries, position)
    for key, value in values.items():
        open_brace, close_brace, entries = _jsonc_top_level(text)
        first = entries[0]["key_start"] if entries else None
        indent = text[_line_start(text, first) : first] if first is not None else ""
        indent = indent if indent and not indent.strip() else "    "
        rendered = json.dumps(value, indent=indent).replace("\n", "\n" + indent)
        existing = next((entry for entry in entries if entry["key"] == key), None)
        if existing is not None:
            text = text[: existing["value_start"]] + rendered + text[existing["value_end"] :]
            continue
        line = f"{indent}{json.dumps(key)}: {rendered}"
        if not entries:
            text = text[: open_brace + 1] + f"\n{line}\n" + text[open_brace + 1 :].lstrip(" \t")
            continue
        last = entries[-1]
        if last["comma_end"] is None:
            text = text[: last["value_end"]] + "," + text[last["value_end"] :]
            close_brace += 1
            after = last["value_end"] + 1
        else:
            after = last["comma_end"]
        # After the last entry's line, so a comment on that line stays with it.
        at = _line_end(text, after)
        at = after if close_brace < at else at
        text = text[:at] + f"\n{line}" + text[at:]
    loaded = loads_jsonc(text)
    if not isinstance(loaded, dict) or any(
        loaded.get(key) != value for key, value in values.items()
    ):
        raise ValueError("the edited file doesn't hold the expected values")
    return text
