"""Disposable local home and explicit subprocess environment, with redacted artifacts."""

import json
import os
import re
from pathlib import Path

from .terminal import Terminal

MANAGED_PATHS = (
    Path("/etc/claude-code/managed-settings.json"),
    Path("/etc/codex/managed_config.toml"),
    Path("/etc/codex/requirements.toml"),
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
)


class UserSession:
    def __init__(self, root, binary, artifacts, bearer):
        self.home, self.project = root / "home", root / "project"
        self.home.mkdir(mode=0o700)
        self.project.mkdir()
        self.binary, self.artifacts, self._bearer = binary, artifacts, bearer
        artifacts.mkdir(parents=True)
        allowed = (
            "PATH",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
        )
        self.env = {key: os.environ[key] for key in allowed if key in os.environ}
        self.env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_CACHE_HOME": str(self.home / ".cache"),
                "XDG_DATA_HOME": str(self.home / ".local/share"),
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "CODEX_HOME": str(self.home / ".codex"),
                "DATABRICKS_CONFIG_FILE": str(self.home / ".databrickscfg"),
                "DATABRICKS_BEARER": bearer,
                "TERM": "xterm-256color",
                "PYTHONNOUSERSITE": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )

    def redact(self, text):
        text = text.replace(self._bearer, "<redacted>")
        return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1<redacted>", text)

    def record(self, name, value):
        (self.artifacts / f"{name}.json").write_text(
            self.redact(json.dumps(value, indent=2)) + "\n"
        )

    def command(self, name, args):
        with Terminal(self, name, args) as terminal:
            terminal.finish(timeout=240)

    def cleanup(self):
        if (self.home / ".ucode").exists():
            self.command("cleanup-revert", ["revert"])
        assert not any(path.exists() for path in MANAGED_PATHS), (
            "ug revert left machine-wide settings; disposable runner must not be reused"
        )
