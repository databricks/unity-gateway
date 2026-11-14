#!/usr/bin/env python3
"""Delete pr-demo release assets for merged or closed pull requests."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

TAG = "pr-demo-assets"
ASSET_PR = re.compile(r"^pr(\d+)-")


def plan_deletions(assets: list[str], pr_states: dict[int, str]) -> list[str]:
    """Keep unrecognized assets and PRs without a confirmed closed state."""
    return [
        name
        for name in assets
        if (match := ASSET_PR.match(name))
        and pr_states.get(int(match.group(1))) in {"MERGED", "CLOSED"}
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print deletions without making them"
    )
    parser.add_argument("--repo", help="owner/name (default: GH_REPO or origin)")
    args = parser.parse_args(argv)

    gh_path = shutil.which("gh")
    if not gh_path:
        print("warning: gh was not found on PATH", file=sys.stderr)
        return 0
    repo = args.repo or os.environ.get("GH_REPO")
    if not repo:
        git_path = shutil.which("git")
        try:
            if not git_path:
                raise OSError("git was not found on PATH")
            remote = subprocess.run(  # noqa: TID251 - standalone stdlib script
                [git_path, "remote", "get-url", "origin"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            match = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", remote)
            if not match:
                raise ValueError("origin is not a GitHub remote")
            repo = match.group(1)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            print(f"warning: cannot determine repository from origin: {error}", file=sys.stderr)
            return 0

    def gh(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: TID251 - standalone stdlib script
            [gh_path, *arguments, "--repo", repo], capture_output=True, text=True, check=False
        )

    try:
        release = gh("release", "view", TAG, "--json", "assets")
    except OSError as error:
        print(f"warning: cannot read release: {error}", file=sys.stderr)
        return 0
    if release.returncode:
        if "release not found" in release.stderr.lower():
            print(f"Release {TAG} does not exist in {repo}; nothing to clean up.")
        else:
            print(f"warning: cannot read release: {release.stderr.strip()}", file=sys.stderr)
        return 0
    try:
        assets = [asset["name"] for asset in json.loads(release.stdout)["assets"]]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"warning: cannot parse release assets: {error}", file=sys.stderr)
        return 0

    numbers: set[int] = set()
    for name in assets:
        match = ASSET_PR.match(name)
        if match:
            numbers.add(int(match.group(1)))
        else:
            print(f"warning: leaving unrecognized asset {name!r}", file=sys.stderr)
    states: dict[int, str] = {}
    for number in sorted(numbers):
        try:
            result = gh("pr", "view", str(number), "--json", "state")
            if result.returncode:
                raise ValueError(result.stderr.strip())
            states[number] = json.loads(result.stdout)["state"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"warning: cannot determine state of PR #{number}: {error}", file=sys.stderr)

    deletions = plan_deletions(assets, states)
    if not deletions:
        print("No closed or merged PR assets to delete.")
    failed = False
    for name in deletions:
        if args.dry_run:
            print(f"dry run: would delete {name}")
            continue
        try:
            result = gh("release", "delete-asset", TAG, name, "--yes")
            if result.returncode:
                raise ValueError(result.stderr.strip())
            print(f"Deleted {name}")
        except (OSError, ValueError) as error:
            print(f"warning: cannot delete {name}: {error}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
