"""Verify the token is gone from every ref while history keeps its shape."""

import os
import subprocess
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))
TOKEN = "ugb_live_7f3a9c2e1d4b8a6f0e5d"
EXPECTED = {
    "main": [
        "Read token from the environment",
        "Use settings in app",
        "Add settings",
        "Initial commit",
    ],
    "feature/report": ["Document deploy", "Use settings in app", "Add settings", "Initial commit"],
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=APP, check=True, capture_output=True, text=True
    ).stdout


def main() -> None:
    for branch, messages in EXPECTED.items():
        assert git("log", "--format=%s", branch).splitlines() == messages, branch

    for commit in git("rev-list", "--all").split():
        found = subprocess.run(["git", "grep", "-F", TOKEN, commit], cwd=APP, capture_output=True)
        assert found.returncode == 1, f"token still in {commit}"
        assert TOKEN not in git("show", "-s", "--format=%B", commit)

    settings = git("show", "main~2:config/settings.py")
    assert "REDACTED" in settings and "TIMEOUT = 30" in settings, settings
    assert "REDACTED" in git("show", "feature/report:docs/deploy.md")
    assert "os.environ" in git("show", "main:config/settings.py")
    assert git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    print("ok")


if __name__ == "__main__":
    main()
