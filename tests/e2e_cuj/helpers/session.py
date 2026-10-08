"""CUJ authentication and cleanup on the existing integration session."""

import re
from dataclasses import dataclass

import pytest

from tests.integration.utils.harness import UserSession as IntegrationSession
from tests.integration.utils.terminal import TerminalProcess

from . import constants

MANAGED_STATE_FILES = ("state.json", "managed-backups/manifest.json")


@dataclass(frozen=True)
class MachineWideLeak:
    culprit: str
    paths: tuple[str, ...]


MACHINE_WIDE_LEAK = pytest.StashKey[MachineWideLeak]()


def record_machine_wide_leak(request):
    """Name the scenario that left machine-wide settings in subsequent setup failures."""
    leaked = tuple(str(path) for path in constants.MANAGED_PATHS if path.exists())
    if leaked:
        request.config.stash[MACHINE_WIDE_LEAK] = MachineWideLeak(request.node.nodeid, leaked)


def dirty_runner_message(leak: MachineWideLeak | None) -> str:
    if leak is None:
        return (
            "Existing machine-wide agent settings; use a clean disposable runner. "
            "Nothing was changed."
        )
    return (
        f"{leak.culprit} left machine-wide agent settings {list(leak.paths)}. Nothing was changed."
    )


class UserSession(IntegrationSession):
    def __init__(self, root, binary, artifacts, bearer):
        super().__init__(root, root, binary, artifacts)
        self.home.chmod(0o700)
        self.env.update(TERM="xterm-256color")
        self.env.pop("NO_COLOR", None)
        self._installed_bearers = set()
        self.install_bearer(bearer)
        self._class_bearer = bearer

    def install_bearer(self, bearer):
        """Use a bearer for later commands; replaced bearers stay redacted in later artifacts."""
        self._installed_bearers.add(bearer)
        self.env["DATABRICKS_BEARER"] = bearer

    def refresh_bearer(self, bearer):
        """Replace an expiring class bearer, unless a journey switched to another workspace's."""
        if self.env["DATABRICKS_BEARER"] == self._class_bearer:
            self._class_bearer = bearer
            self.install_bearer(bearer)

    def redact(self, text, *, strip_ansi=False):
        text = super().redact(text, strip_ansi=strip_ansi)
        for token in self._installed_bearers:
            text = text.replace(token, "<redacted>")
        return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1<redacted>", text)

    def configure(self, args):
        """Configure without a PTY so CI uses isolated user-local settings."""
        self.run(*args, timeout=240)
        assert not any(path.exists() for path in constants.MANAGED_PATHS), (
            "Non-interactive CUJ configure unexpectedly created machine-wide settings"
        )

    def revert_machine_wide(self, name, message):
        state_dir = self.home / ".ucode"
        if any((state_dir / file).is_file() for file in MANAGED_STATE_FILES):
            with TerminalProcess(self, "ug", [str(self.binary), "revert"], name) as terminal:
                terminal.finish()
        assert not any(path.exists() for path in constants.MANAGED_PATHS), message
