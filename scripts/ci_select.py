"""Decide whether a merge group's live agent and integration suites run, and record it as `live`.

The live suites drive real agents with CI secrets against shared workspaces. They run in the
merge queue, on main, on the schedule and on dispatch; pull requests never run them.

`merge-group <base> <head> <repository>` runs every live suite when every PR in the group comes
from this repository. A fork PR's queued code never receives CI secrets here, matching its PR
runs; members authorize fork integration with `/integration-test`.

CI runs this file from the base revision, so a PR cannot change how its own run is selected.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# The merge queue squashes each PR into a commit whose subject ends in "(#<number>)".
SQUASH_SUBJECT = re.compile(r"\(#(\d+)\)$")


def queued_prs(base: str, head: str) -> list[str]:
    """PR numbers squashed into the merge group, one per commit."""
    subjects = subprocess.run(
        ["git", "log", "--reverse", "--format=%s", f"{base}..{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    numbers = [SQUASH_SUBJECT.search(subject) for subject in subjects]
    if not subjects or None in numbers:
        raise SystemExit(f"::error::Cannot attribute every merge group commit to a PR: {subjects}")
    return [match[1] for match in numbers]


def pr_is_from(number: str, repository: str) -> bool:
    """Whether PR ``number`` comes from ``repository`` rather than a fork.

    The request is anonymous: hosted runners' job tokens are rejected by the organization's IP
    allow list. A merge group makes one request per queued PR.
    """
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    url = f"{api}/repos/{repository}/pulls/{number}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            pull = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise SystemExit(
            f"::error::Could not look up PR #{number} ({error}); the merge queue cannot tell "
            "whether its code may receive CI secrets. Re-queue the PR once GitHub's API answers."
        ) from error
    head_repo = (pull.get("head") or {}).get("repo") or {}
    print(f"PR #{number} comes from {head_repo.get('full_name') or 'a deleted fork'}")
    return head_repo.get("full_name") == repository


def main() -> None:
    mode, *args = sys.argv[1:]
    if mode != "merge-group":
        raise SystemExit(f"::error::Unknown selection mode {mode!r}.")
    base, head, repository = args
    live = all(pr_is_from(number, repository) for number in queued_prs(base, head))
    print(f"live suites {'run' if live else 'skipped: fork code gets no CI secrets'}")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"live={'true' if live else 'false'}\n")


if __name__ == "__main__":
    main()
