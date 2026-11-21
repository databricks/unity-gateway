"""Decide whether a pull request's diff can reach the live agent and integration suites.

The live suites drive real agents against shared workspaces, so a PR whose changes cannot
reach them gets its feedback from lint and the unit suites alone. Every file outside
CANNOT_REACH_LIVE reruns everything. The merge queue, main and scheduled runs always run the
full live suites, so a wrong entry here is caught before it reaches main.
"""

import fnmatch
import subprocess
import sys

# Paths whose changes no live journey can observe. Keep this list short and literal.
CANNOT_REACH_LIVE = (
    "docs/*",
    "specs/*",
    "bench/*",
    "LICENSE.md",
    "NOTICE.md",
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "hk.pkl",
    "tests/*.md",
    "tests/integration/*.md",
    "tests/e2e_cuj/*.md",
    "tests/*.mjs",
    "tests/test_*.py",
)
# Unit-suite paths that the live E2E shards do run.
LIVE_EXCEPTIONS = ()  # Deliberately wrong: the selection proof hides tests/test_e2e.py.


def reaches_live(paths: list[str]) -> bool:
    """True unless every changed path is one no live journey can observe."""
    for path in paths:
        if path in LIVE_EXCEPTIONS:
            return True
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in CANNOT_REACH_LIVE):
            return True
    return not paths


def main() -> None:
    base, head = sys.argv[1:3]
    changed = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    live = reaches_live(changed)
    print(f"{len(changed)} changed files; live suites {'run' if live else 'skipped'}")
    for path in changed:
        print(f"  {path}")
    print(f"live={'true' if live else 'false'}")


if __name__ == "__main__":
    main()
