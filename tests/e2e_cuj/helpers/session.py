"""CUJ authentication and cleanup on the existing integration session."""

import re

from tests.integration.utils.harness import UserSession as IntegrationSession

from . import constants

try:
    from tests.integration.utils.terminal import TerminalProcess
except ModuleNotFoundError as error:
    if error.name not in {"pexpect", "pyte"}:
        raise
    TerminalProcess = None


class UserSession(IntegrationSession):
    def __init__(self, root, binary, artifacts, bearer):
        super().__init__(root, root, binary, artifacts)
        self.home.chmod(0o700)
        self.env.update(DATABRICKS_BEARER=bearer, TERM="xterm-256color")
        self.env.pop("NO_COLOR", None)

    def redact(self, text, *, strip_ansi=False):
        text = super().redact(text, strip_ansi=strip_ansi)
        return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1<redacted>", text)

    def command(self, name, args):
        if TerminalProcess is None:
            raise RuntimeError("Full TUI CUJs require the pexpect and pyte test dependencies")
        with TerminalProcess(self, "ug", [str(self.binary), *args], name) as terminal:
            terminal.finish(timeout=240)

    def configure(self, args):
        """Configure without a PTY so CI uses isolated user-local settings."""
        self.run(*args, timeout=240)
        assert not any(path.exists() for path in constants.MANAGED_PATHS), (
            "Non-interactive CUJ configure unexpectedly created machine-wide settings"
        )

    def cleanup(self):
        if (self.home / ".ucode").exists():
            self.command("cleanup-revert", ["revert"])
        assert not any(path.exists() for path in constants.MANAGED_PATHS), (
            "ug revert left machine-wide settings; disposable runner must not be reused"
        )
