"""Packaging for the native Claude mod entry point."""

import json

from ucode.agents.claude_mod import HOOKS_SOURCE, write_hooks


def test_mod_entry_point_is_packaged(tmp_path):
    write_hooks(tmp_path)

    assert json.loads((tmp_path / "hooks/hooks.json").read_text()) == {"modules": ["./register.ts"]}
    assert (tmp_path / "hooks/register.ts").read_bytes() == (
        HOOKS_SOURCE / "register.ts"
    ).read_bytes()
    assert sorted(path.name for path in (tmp_path / "hooks").iterdir()) == [
        "hooks.json",
        "register.ts",
    ]
