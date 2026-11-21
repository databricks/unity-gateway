"""Decide whether a CI run's live agent and integration suites run, and record it as `live`.

The live suites drive real agents with CI secrets against shared workspaces.

- `pull-request <base> <head>`: a PR whose diff cannot reach them gets its feedback from lint and
  the unit suites alone. Every file outside CANNOT_REACH_LIVE reruns everything.
- `merge-group <head ref> <repository>`: the merge queue runs every live suite for a PR from
  this repository. A fork PR's queued code never receives CI secrets here, matching its PR runs;
  members authorize fork integration with `/integration-test`.

The merge queue, main and scheduled runs therefore run the full live suites for every same-repo
change, so a wrong entry in CANNOT_REACH_LIVE turns the merge queue red before it reaches main.
"""

import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.request

# Paths whose changes no live journey can observe. Keep this list short and literal. A trailing
# `/**` covers a whole tree; every other `*` matches within one path segment only. Root agent
# instructions (AGENTS.md, CLAUDE.md) are not here: live launches run in the checkout and read them.
CANNOT_REACH_LIVE = (
    "docs/**",
    "specs/**",
    "bench/**",
    "LICENSE.md",
    "NOTICE.md",
    "README.md",
    "hk.pkl",
    "tests/*.md",
    "tests/integration/*.md",
    "tests/e2e_cuj/*.md",
    "tests/*.mjs",
    "tests/test_*.py",
)
# Unit-suite paths that the live E2E shards do run.
LIVE_EXCEPTIONS = ("tests/test_e2e.py",)
MERGE_GROUP_REF = re.compile(r"gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]{40}")


def reaches_live(paths: list[str]) -> bool:
    """True unless every changed path is one no live journey can observe."""
    for path in paths:
        if path in LIVE_EXCEPTIONS:
            return True
        if not any(_matches(path, pattern) for pattern in CANNOT_REACH_LIVE):
            return True
    return not paths


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return path.count("/") == pattern.count("/") and fnmatch.fnmatchcase(path, pattern)


def changed_paths(base: str, head: str) -> list[str]:
    """Paths changed on the PR, NUL-separated so names with spaces stay whole."""
    output = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [path for path in output.split("\0") if path]


def queued_pr_is_from(head_ref: str, repository: str) -> bool:
    """Whether the merge group's PR comes from ``repository`` rather than a fork."""
    match = MERGE_GROUP_REF.fullmatch(head_ref)
    if match is None:
        raise SystemExit(f"::error::Unrecognized merge group ref {head_ref!r}.")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    url = f"{api}/repos/{repository}/pulls/{match[1]}"
    with urllib.request.urlopen(url, timeout=30) as response:
        pull = json.load(response)
    head_repo = (pull.get("head") or {}).get("repo") or {}
    print(f"Merge group for PR #{match[1]} from {head_repo.get('full_name') or 'a deleted fork'}")
    return head_repo.get("full_name") == repository


def main() -> None:
    mode, *args = sys.argv[1:]
    if mode == "pull-request":
        changed = changed_paths(*args)
        live = reaches_live(changed)
        print(f"{len(changed)} changed files; live suites {'run' if live else 'skipped'}")
        for path in changed:
            print(f"  {path}")
    elif mode == "merge-group":
        live = queued_pr_is_from(*args)
        print(f"live suites {'run' if live else 'skipped: fork code gets no CI secrets'}")
    else:
        raise SystemExit(f"::error::Unknown selection mode {mode!r}.")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"live={'true' if live else 'false'}\n")


if __name__ == "__main__":
    main()
