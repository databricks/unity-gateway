"""Temporarily restrict native search for a Cowork MCP test session."""

import argparse
import json
import os
from pathlib import Path


def apply(profile: Path, backup: Path) -> None:
    original = profile.read_bytes()
    settings = json.loads(original.decode("utf-8-sig"))
    disabled = settings.get("disabledBuiltinTools", [])
    if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
        raise ValueError("Desktop disabledBuiltinTools must be a list of strings")
    settings["disabledBuiltinTools"] = list(dict.fromkeys([*disabled, "WebSearch"]))
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(original)
    profile.write_text(json.dumps(settings), encoding="utf-8")


def restore(profile: Path, backup: Path) -> None:
    profile.write_bytes(backup.read_bytes())
    backup.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "restore"))
    parser.add_argument("profile", type=Path)
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    {"apply": apply, "restore": restore}[args.action](args.profile, args.backup)
