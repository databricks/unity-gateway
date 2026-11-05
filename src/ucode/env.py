"""Shared environment flag parsing."""

import os

from ucode.constants import TRUE_ENV_VALUES


def env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_ENV_VALUES
