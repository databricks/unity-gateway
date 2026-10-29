"""Shared environment flag parsing."""

import os

from ucode.constants import TRUE_ENV_VALUES


def env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_ENV_VALUES


# Set by a wrapper that starts ug in a terminal nobody watches (e.g. an unattended relaunch).
NONINTERACTIVE_ENV = "UG_NONINTERACTIVE"


def unattended_terminal() -> bool:
    """Whether a wrapper marked this run's terminal as one nobody is watching.

    Prompts there (a sudo password, a browser sign-in) would block until the wrapper gives up.
    """
    return env_bool(NONINTERACTIVE_ENV)
