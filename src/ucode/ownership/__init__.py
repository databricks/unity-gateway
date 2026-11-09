"""Ownership tables: which agent settings keys Unity Gateway owns in machine-wide files.

Each agent has one TOML table shipped with the package (``claude.toml``, ``codex.toml``). The
table is the single source of truth for:

- ``ug configure --global`` on a workspace that publishes an agent configuration, which builds an agent's machine-wide file from four layers
  (UG defaults < admin native settings < feature requirements < gateway requirements) with
  :func:`compose`;
- the generated admin docs (``python -m ucode.ownership --write docs/admin-managed-settings.md``),
  which CI checks with ``--check``.

Keys not in a table pass through from the admin's native settings unchanged, so admins can use a
new agent setting the day it ships.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources

from ucode.managed_files import _leaf_paths, _overlaps, _set_path_value

RESERVED = "reserved"
CONDITIONALLY_RESERVED = "conditionally_reserved"
DEFAULT = "default"
SHARED_LIST = "shared_list"
SHARED_TABLE = "shared_table"
CLASSES = (RESERVED, CONDITIONALLY_RESERVED, DEFAULT, SHARED_LIST, SHARED_TABLE)
FEATURES = ("tracing", "models", "smart_routing")

# Table section names per agent, in display order. Claude has one managed file; Codex has the
# enforced requirements file and the system config defaults file.
SECTIONS = {
    "claude": ("entry",),
    "codex": ("requirement", "setting"),
}
SECTION_TITLES = {
    "entry": "`agent_native_settings` (managed-settings.json)",
    "requirement": "`agent_native_requirements` (/etc/codex/requirements.toml)",
    "setting": "`agent_native_settings` (/etc/codex/config.toml)",
}

# Report outcomes for one admin-supplied key.
APPLIED = "applied"
SKIPPED = "skipped"
OVERRODE_DEFAULT = "overrode Unity Gateway default"


@dataclass(frozen=True)
class Entry:
    """One key Unity Gateway treats specially."""

    path: tuple[str, ...]
    cls: str
    note: str
    feature: str | None = None
    in_managed_file: bool = True
    value: object = None

    @property
    def dotted(self) -> str:
        return ".".join(self.path)

    def active(self, features: set[str]) -> bool:
        """Whether UG owns this key in a run with ``features`` turned on."""
        if self.cls == RESERVED:
            return True
        if self.cls == CONDITIONALLY_RESERVED:
            return self.feature in features
        return False


@dataclass(frozen=True)
class Table:
    agent: str
    display: str
    sections: dict[str, tuple[Entry, ...]]

    def section(self, name: str) -> tuple[Entry, ...]:
        return self.sections[name]


@dataclass(frozen=True)
class ReportItem:
    outcome: str
    path: str
    note: str = ""


@dataclass
class Composed:
    """A composed machine-wide document and what happened to each admin key."""

    doc: dict
    items: list[ReportItem] = field(default_factory=list)


def _parse_entry(raw: dict, *, source: str) -> Entry:
    path = raw.get("path")
    cls = raw.get("class")
    note = raw.get("note")
    if not isinstance(path, str) or not path or not all(path.split(".")):
        raise ValueError(f"{source}: entry has an invalid path: {path!r}")
    if cls not in CLASSES:
        raise ValueError(f"{source}: {path} has an unknown class {cls!r}")
    if not isinstance(note, str) or not note.strip():
        raise ValueError(f"{source}: {path} needs a note")
    feature = raw.get("feature")
    if cls == CONDITIONALLY_RESERVED:
        if feature not in FEATURES:
            raise ValueError(f"{source}: {path} needs a feature, one of {', '.join(FEATURES)}")
    elif feature is not None:
        raise ValueError(f"{source}: only conditionally_reserved entries take a feature ({path})")
    if cls == DEFAULT and "value" not in raw:
        raise ValueError(f"{source}: default entry {path} needs a value")
    if cls != DEFAULT and "value" in raw:
        raise ValueError(f"{source}: only default entries take a value ({path})")
    in_file = raw.get("in_managed_file", True)
    if not isinstance(in_file, bool):
        raise ValueError(f"{source}: in_managed_file must be true or false ({path})")
    unknown = set(raw) - {"path", "class", "note", "feature", "in_managed_file", "value"}
    if unknown:
        raise ValueError(f"{source}: {path} has unknown fields: {', '.join(sorted(unknown))}")
    return Entry(
        path=tuple(path.split(".")),
        cls=cls,
        note=note.strip(),
        feature=feature,
        in_managed_file=in_file,
        value=raw.get("value"),
    )


def load_table(agent: str) -> Table:
    """Load and validate ``agent``'s ownership table from the package data."""
    if agent not in SECTIONS:
        raise ValueError(f"No ownership table for {agent!r}")
    source = f"{agent}.toml"
    data = tomllib.loads(resources.files(__name__).joinpath(source).read_text(encoding="utf-8"))
    sections: dict[str, tuple[Entry, ...]] = {}
    for name in SECTIONS[agent]:
        raw_entries = data.get(name) or []
        entries = tuple(_parse_entry(raw, source=source) for raw in raw_entries)
        seen: set[tuple[str, ...]] = set()
        for entry in entries:
            if entry.path in seen:
                raise ValueError(f"{source}: {entry.dotted} is listed twice under {name}")
            seen.add(entry.path)
        sections[name] = entries
    return Table(agent=agent, display=str(data.get("display") or agent), sections=sections)


def entries_by_class(entries: tuple[Entry, ...], cls: str) -> list[Entry]:
    return [entry for entry in entries if entry.cls == cls]


def _overlapping(entries: tuple[Entry, ...], path: list[str]) -> list[Entry]:
    return [entry for entry in entries if _overlaps(list(entry.path), path)]


def compose(
    entries: tuple[Entry, ...],
    *,
    native: dict | None,
    ug_values: dict,
    features: set[str],
    ug_table_names: dict[tuple[str, ...], set[str]] | None = None,
) -> Composed:
    """Build one machine-wide document from scratch.

    ``native`` is the admin's raw native settings, ``ug_values`` everything UG writes this run
    (gateway and feature requirements), ``features`` the UG features turned on, and
    ``ug_table_names`` the entry names UG registers per ``shared_table`` path.

    Raises ``ValueError`` when ``ug_values`` would write a key the table doesn't let UG write:
    that is a bug in UG, which the tests keep from shipping.
    """
    ug_table_names = ug_table_names or {}
    native = native if isinstance(native, dict) else {}
    result = Composed(doc={})
    _check_ug_values(entries, ug_values, features)

    kept: list[tuple[list[str], object]] = []
    admin_lists: dict[tuple[str, ...], list] = {}
    overridden_defaults: set[tuple[str, ...]] = set()
    for path, value in _leaf_paths(native, []):
        dotted = ".".join(path)
        matches = _overlapping(entries, path)
        owner = next((entry for entry in matches if entry.active(features)), None)
        if owner is not None:
            result.items.append(ReportItem(SKIPPED, dotted, owner.note))
            continue
        shared = next((e for e in matches if e.cls in (SHARED_LIST, SHARED_TABLE)), None)
        if shared is not None and len(path) < len(shared.path):
            # Replacing the parent would replace the entries UG adds to the shared value.
            result.items.append(ReportItem(SKIPPED, dotted, shared.note))
            continue
        if shared is not None and shared.cls == SHARED_LIST and tuple(path) == shared.path:
            if isinstance(value, list):
                admin_lists[shared.path] = list(value)
                result.items.append(ReportItem(APPLIED, dotted))
                continue
            result.items.append(ReportItem(SKIPPED, dotted, f"Must be a list. {shared.note}"))
            continue
        if shared is not None and shared.cls == SHARED_TABLE and len(path) > len(shared.path):
            name = path[len(shared.path)]
            if name in ug_table_names.get(shared.path, set()):
                result.items.append(
                    ReportItem(SKIPPED, ".".join(path[: len(shared.path) + 1]), shared.note)
                )
                continue
        default = next((e for e in matches if e.cls == DEFAULT), None)
        if default is not None:
            overridden_defaults.add(default.path)
            result.items.append(ReportItem(OVERRODE_DEFAULT, dotted, default.note))
        else:
            result.items.append(ReportItem(APPLIED, dotted))
        kept.append((path, value))

    # Layer 1: UG defaults the admin didn't set.
    for entry in entries_by_class(entries, DEFAULT):
        if entry.in_managed_file and entry.path not in overridden_defaults:
            _set_path_value(result.doc, list(entry.path), entry.value)
    # Layer 2: the admin's native settings.
    for path, value in kept:
        _set_path_value(result.doc, path, value)
    # Layers 3 and 4: what UG requires, merged into shared values rather than replacing them.
    for path, value in _leaf_paths(ug_values, []):
        shared = next(
            (
                e
                for e in _overlapping(entries, path)
                if e.cls in (SHARED_LIST, SHARED_TABLE) and tuple(path) == e.path
            ),
            None,
        )
        if shared is not None and shared.cls == SHARED_LIST and isinstance(value, list):
            admin_items = admin_lists.pop(shared.path, [])
            merged = list(admin_items)
            merged.extend(item for item in value if item not in merged)
            _set_path_value(result.doc, path, merged)
            continue
        _set_path_value(result.doc, path, value)
    for list_path, items in admin_lists.items():
        _set_path_value(result.doc, list(list_path), items)
    return result


def _check_ug_values(entries: tuple[Entry, ...], ug_values: dict, features: set[str]) -> None:
    """Every key UG writes must be one the table lets it write in the managed file."""
    for path, _value in _leaf_paths(ug_values, []):
        matches = [
            entry
            for entry in _overlapping(entries, path)
            if len(entry.path) <= len(path) and entry.in_managed_file
        ]
        if not any(
            entry.active(features) or entry.cls in (SHARED_LIST, SHARED_TABLE) for entry in matches
        ):
            raise ValueError(
                f"Unity Gateway writes {'.'.join(path)}, which its ownership table doesn't "
                "reserve for the managed file. Add it to the table."
            )


def defaults(entries: tuple[Entry, ...]) -> dict[str, object]:
    return {entry.dotted: entry.value for entry in entries_by_class(entries, DEFAULT)}


# ---------------------------------------------------------------------------------------------
# Generated admin docs
# ---------------------------------------------------------------------------------------------

DOCS_BEGIN = "<!-- BEGIN GENERATED OWNERSHIP TABLES: run `uv run python -m ucode.ownership --write docs/admin-managed-settings.md` -->"
DOCS_END = "<!-- END GENERATED OWNERSHIP TABLES -->"

_CLASS_LABELS = {
    RESERVED: "Reserved",
    CONDITIONALLY_RESERVED: "Reserved while a feature is on",
    DEFAULT: "Unity Gateway default",
    SHARED_LIST: "Shared list",
    SHARED_TABLE: "Shared table",
}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _render_section(entries: tuple[Entry, ...]) -> list[str]:
    lines = [
        "| Key | Class | Feature | Written by Unity Gateway | Value | Note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        value = f"`{entry.value!r}`".replace("'", '"') if entry.cls == DEFAULT else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{entry.dotted}`",
                    _CLASS_LABELS[entry.cls],
                    f"`{entry.feature}`" if entry.feature else "",
                    "Yes" if entry.in_managed_file else "No",
                    value,
                    _cell(entry.note),
                ]
            )
            + " |"
        )
    return lines


def render_docs() -> str:
    """The generated ownership-table section of the admin docs."""
    lines = [DOCS_BEGIN, ""]
    for agent in SECTIONS:
        table = load_table(agent)
        for name in SECTIONS[agent]:
            lines += [f"### {table.display}: {SECTION_TITLES[name]}", ""]
            lines += _render_section(table.section(name))
            lines.append("")
    lines.append(DOCS_END)
    return "\n".join(lines)


def replace_docs_section(text: str) -> str:
    """``text`` with its generated section replaced by the current tables."""
    begin = text.find(DOCS_BEGIN)
    end = text.find(DOCS_END)
    if begin == -1 or end == -1 or end < begin:
        raise ValueError("The docs file has no generated ownership-table markers.")
    return text[:begin] + render_docs() + text[end + len(DOCS_END) :]
