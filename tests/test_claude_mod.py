"""Generated plugin contents for the native Claude mod entry point."""

import json
from pathlib import Path

from ucode.smart_routing import v2


def test_plugin_contains_mod_entry_point(tmp_path):
    v2._write_routed_claude_plugin(tmp_path, [])

    assert json.loads((tmp_path / "hooks/hooks.json").read_text()) == {"modules": ["./register.ts"]}
    assert (tmp_path / "hooks/register.ts").read_bytes() == (
        Path(v2.__file__).parents[1] / "agents" / "claude_mods" / "register.ts"
    ).read_bytes()
    assert sorted(path.name for path in (tmp_path / "hooks").iterdir()) == [
        "hooks.json",
        "register.ts",
    ]
