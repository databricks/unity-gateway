"""Build a git repo at <app> that leaks a token in history on two branches."""

import subprocess
import sys
from pathlib import Path

TOKEN = "ugb_live_7f3a9c2e1d4b8a6f0e5d"


def git(app: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=app, check=True, capture_output=True)


def commit(app: Path, message: str, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, newline="\n")
    git(app, "add", "-A")
    git(app, "commit", "-m", message)


def main(app: Path) -> None:
    app.mkdir(parents=True, exist_ok=True)
    git(app, "init", "-q", "-b", "main")
    for key, value in [
        ("user.name", "Bench Bot"),
        ("user.email", "bench@example.com"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ]:
        git(app, "config", key, value)

    commit(app, "Initial commit", {"README.md": "# report service\n", "app.py": "print('hi')\n"})
    commit(app, "Add settings", {"config/settings.py": f'API_TOKEN = "{TOKEN}"\nTIMEOUT = 30\n'})
    commit(
        app,
        "Use settings in app",
        {"app.py": "from config.settings import API_TOKEN, TIMEOUT\n\nprint(TIMEOUT)\n"},
    )
    git(app, "branch", "feature/report")
    commit(
        app,
        "Read token from the environment",
        {"config/settings.py": 'import os\n\nAPI_TOKEN = os.environ["API_TOKEN"]\nTIMEOUT = 30\n'},
    )
    git(app, "checkout", "-q", "feature/report")
    commit(
        app,
        "Document deploy",
        {"docs/deploy.md": f"Deploy with:\n\n    API_TOKEN={TOKEN} python app.py\n"},
    )
    git(app, "checkout", "-q", "main")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
