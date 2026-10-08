"""CUJ authentication and cleanup on the existing integration session."""

import os
import re

from tests.integration.utils.harness import UserSession as IntegrationSession

from . import constants


class UserSession(IntegrationSession):
    def __init__(self, root, binary, artifacts, bearer):
        super().__init__(root, root, binary, artifacts)
        self.home.chmod(0o700)
        self.env.update(DATABRICKS_BEARER=bearer, TERM="xterm-256color")
        self.env.pop("NO_COLOR", None)

    def redact(self, text, *, strip_ansi=False):
        text = super().redact(text, strip_ansi=strip_ansi)
        # CI uploads CUJ artifacts, so also scrub every *SECRET* env var.
        for name, secret in os.environ.items():
            if "SECRET" in name.upper() and secret:
                text = text.replace(secret, "<redacted>")
        return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1<redacted>", text)

    def configure(self, args):
        """Configure without a PTY so CI uses isolated user-local settings."""
        self.run(*args, timeout=240)
        assert not any(path.exists() for path in constants.MANAGED_PATHS), (
            "Non-interactive CUJ configure unexpectedly created machine-wide settings"
        )
