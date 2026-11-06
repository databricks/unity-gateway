import os
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from ucode.os_compatibility import subprocess_cross_os as process


def main():
    if sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Use a disposable macOS GitHub Actions runner")
    python = shutil.which("python3.12")
    if not python:
        raise SystemExit("Runner image has no Python 3.12")
    temp = Path(os.environ["RUNNER_TEMP"])
    runtime = temp / "macos-desktop-runtime"
    process.run([python, "-m", "venv", str(runtime)], check=True)
    python = str(runtime / "bin/python")
    user = quote(os.environ["JFROG_USER"], safe="")
    token = quote(os.environ["JFROG_TOKEN"], safe="")
    env = dict(os.environ)
    env.update(
        PIP_CONFIG_FILE=os.devnull,
        PIP_INDEX_URL=f"https://{user}:{token}@databricks.jfrog.io/artifactory/api/pypi/db-pypi/simple",
        PIP_EXTRA_INDEX_URL="",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    process.run([python, "-m", "pip", "install", "."], env=env, check=True)
    npmrc = temp / "macos-desktop.npmrc"
    npmrc.write_text(
        "registry=https://databricks.jfrog.io/artifactory/api/npm/db-npm/\n"
        "//databricks.jfrog.io/artifactory/api/npm/db-npm/:_authToken=${JFROG_TOKEN}\n"
    )
    try:
        npm = shutil.which("npm")
        if not npm:
            raise SystemExit("Runner image has no npm")
        process.run(
            [
                npm,
                "install",
                "-g",
                "@anthropic-ai/claude-code@2.1.290",
                "--no-audit",
                "--fund=false",
                "--userconfig",
                str(npmrc),
            ],
            check=True,
        )
    finally:
        npmrc.unlink(missing_ok=True)
    with Path(os.environ["GITHUB_ENV"]).open("a") as output:
        output.write(f"UG_PROBE_PYTHON={python}\n")


if __name__ == "__main__":
    main()
