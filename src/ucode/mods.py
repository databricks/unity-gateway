"""Shared infrastructure for shipping Claude Code "mods".

A Claude Mod (Claude Code >= 2.1.290) is a plugin whose ``hooks/hooks.json``
lists a ``modules`` entry pointing at an in-process TypeScript module that draws
in the TUI. ug keeps each mod's source as a real ``.ts`` file under
``agents/claude_mods/`` (shipped with the package) and
copies it into a launch-scoped plugin's ``hooks/`` on demand.

Adding a mod going forward:
  1. Drop ``agents/claude_mods/<name>.ts`` (exporting ``register``).
  2. Register it here: ``MY_MOD = ClaudeMod(source="<name>.ts")``.
  3. Write it into a plugin dir with ``write_mod(plugin_dir, MY_MOD)``.

Subagent display gating lives in :mod:`ucode.constants` (``ENABLE_CLAUDE_CODE_MODS`` /
``claude_code_mods_enabled``); the Claude version floor lives in :mod:`ucode.agents.claude`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ucode.config_io import write_json_file, write_text_file


@dataclass(frozen=True)
class ClaudeMod:
    """A mod ug can write into a launch-scoped plugin.

    ``source`` is the entry ``.ts`` under ``agents/claude_mods/`` (also the
    hooks module filename). ``extra`` names sibling files it imports (e.g. a
    shared ``pets.ts``); they are copied alongside so relative imports resolve.
    """

    source: str
    extra: tuple[str, ...] = ()


def _mods_source_dir() -> Path:
    return Path(__file__).parent / "agents" / "claude_mods"


def write_mod(plugin_dir: Path, mod: ClaudeMod) -> None:
    """Copy ``mod``'s TypeScript (entry + any siblings) into ``plugin_dir``'s hooks."""
    source_dir = _mods_source_dir()
    hooks_dir = plugin_dir / "hooks"
    for name in (mod.source, *mod.extra):
        write_text_file(hooks_dir / name, (source_dir / name).read_text(encoding="utf-8"))
    write_json_file(hooks_dir / "hooks.json", {"modules": [f"./{mod.source}"]})


# ug's smart-routing UI mod: a register.ts entry that composes the concern
# modules it imports (the status band and subagent routing display).
SMART_ROUTING_UI = ClaudeMod(
    source="register.ts",
    extra=("constants.ts", "smart-routing-status.ts", "subagent-routing.ts"),
)
