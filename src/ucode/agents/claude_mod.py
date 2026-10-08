"""Package the in-process Claude mod into the existing launch-scoped plugin."""

from __future__ import annotations

from pathlib import Path

from ucode.config_io import write_json_file, write_text_file

HOOKS_SOURCE = Path(__file__).with_name("claude_mod_hooks")


def write_hooks(plugin_dir: Path) -> None:
    """Add the packaged mod entry point to a launch-scoped Claude plugin."""
    hooks = plugin_dir / "hooks"
    write_text_file(
        hooks / "register.ts", (HOOKS_SOURCE / "register.ts").read_text(encoding="utf-8")
    )
    write_json_file(hooks / "hooks.json", {"modules": ["./register.ts"]})
