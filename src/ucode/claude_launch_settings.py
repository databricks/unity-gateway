"""Private settings files for a single Claude launch."""

from __future__ import annotations

from pathlib import Path

from ucode.config_io import write_json_file


def write_launch_settings(directory: Path, settings: dict) -> Path:
    path = directory / "settings.json"
    path.touch(mode=0o600)
    write_json_file(path, settings)
    return path
