"""Shared infrastructure for shipping Claude Code "mods".

A Claude Mod (Claude Code >= 2.1.287) is a plugin whose ``hooks/hooks.json``
lists a ``modules`` entry pointing at an in-process TypeScript module that draws
in the TUI. ug keeps each mod's source as a real ``.ts`` file under
``typescript/claude-mods/`` at the repo root (shipped with the package) and
copies it into a launch-scoped plugin's ``hooks/`` on demand.

Adding a mod going forward:
  1. Drop ``typescript/claude-mods/<name>.ts`` (exporting ``register``).
  2. Register it here: ``MY_MOD = ClaudeMod(source="<name>.ts")``.
  3. Write it into a plugin dir with ``write_mod(plugin_dir, MY_MOD)``.

Gating lives in :mod:`ucode.constants` (``ENABLE_CLAUDE_CODE_MODS`` /
``claude_code_mods_enabled``); the Claude version floor is here since it is a
mods-wide requirement, not a smart-routing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path

from ucode.config_io import write_json_file, write_text_file

# Claude Mods (hooks modules in installed plugins) landed in Claude Code 2.1.287.
MINIMUM_CLAUDE_VERSION_FOR_MODS = (2, 1, 287)
MINIMUM_CLAUDE_VERSION_FOR_MODS_TEXT = "2.1.287"

_SOURCE_SUBDIR = "claude-mods"


@dataclass(frozen=True)
class ClaudeMod:
    """A mod ug can write into a launch-scoped plugin.

    ``source`` is the entry ``.ts`` under ``typescript/claude-mods/`` (also the
    hooks module filename). ``extra`` names sibling files it imports (e.g. a
    shared ``pets.ts``); they are copied alongside so relative imports resolve.
    """

    source: str
    extra: tuple[str, ...] = ()


def _mods_source_dir() -> Path:
    # Prefer the live repo tree in a dev/editable checkout (so edits to the TS
    # take effect without a rebuild); fall back to the copy bundled in the wheel.
    repo = Path(__file__).resolve().parents[2] / "typescript" / _SOURCE_SUBDIR
    if repo.is_dir():
        return repo
    return Path(str(distribution("unity-gateway").locate_file("typescript"))) / _SOURCE_SUBDIR


def write_mod(plugin_dir: Path, mod: ClaudeMod) -> None:
    """Copy ``mod``'s TypeScript (entry + any siblings) into ``plugin_dir``'s hooks."""
    source_dir = _mods_source_dir()
    hooks_dir = plugin_dir / "hooks"
    for name in (mod.source, *mod.extra):
        write_text_file(hooks_dir / name, (source_dir / name).read_text(encoding="utf-8"))
    write_json_file(hooks_dir / "hooks.json", {"modules": [f"./{mod.source}"]})


# ug's smart-routing UI mod: a register.ts entry that composes the concern
# modules it imports (today the status band).
SMART_ROUTING_UI = ClaudeMod(
    source="register.ts",
    extra=("smart-routing-status.ts", "subagent-routing.ts"),
)
