#!/usr/bin/env python3
"""Publish pr-demo recordings to a pull request.

Re-runs the secret scan, pushes the gifs to the orphan `pr-demo-assets` branch
of databricks/unity-gateway using git plumbing in a temporary index (no
worktree, no checkout, the developer's index is untouched), edits the PR body
between the `<!-- pr-demo:start ... -->` / `<!-- pr-demo:end -->` markers, and
adds the `pr-demo-recorded` label. `--dry-run` prints the new PR body block
without any network write.

Usage:
    uv run python scripts/pr_demo/publish.py --out pr-demo-out [--pr 123] [--dry-run]
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import record  # same directory; provides the shared secret-scan helpers

ASSETS_BRANCH = "pr-demo-assets"
LABEL = "pr-demo-recorded"
START_MARKER = "<!-- pr-demo:start"
END_MARKER = "<!-- pr-demo:end -->"
PR_BLOCK_RE = re.compile(
    re.escape(START_MARKER) + r"[^>]*-->.*?" + re.escape(END_MARKER) + r"\n?",
    re.DOTALL,
)
GIF_NAMES = ("before.gif", "after.gif")
PNG_NAMES = ("before.png", "after.png")


def run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None):
    return subprocess.run(cmd, check=check, capture_output=True, text=True, env=env)  # noqa: TID251 - runs before ug is installed


def git(*args: str, check: bool = True) -> str:
    return run(["git", *args], check=check).stdout.strip()


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    gh_path = shutil.which("gh")
    if not gh_path:
        raise SystemExit("gh was not found on PATH; install the GitHub CLI first")
    return run([gh_path, *args], check=check)


# ---------------------------------------------------------------------------
# Pure helpers (covered by tests/test_pr_demo.py)
# ---------------------------------------------------------------------------


def asset_url(repo_slug: str, pr: int, short_sha: str, name: str) -> str:
    """Raw github.com URL for a gif on the pr-demo-assets branch."""
    return f"https://github.com/{repo_slug}/raw/{ASSETS_BRANCH}/{pr}/{short_sha}/{name}"


def capture_section(before_text: str, after_text: str) -> str:
    """Collapsed text captures: a diff when the sides differ, plain text otherwise."""
    if before_text == after_text:
        fence = "text"
        body = before_text.strip("\n")
    else:
        fence = "diff"
        body = "\n".join(
            difflib.unified_diff(
                before_text.splitlines(),
                after_text.splitlines(),
                fromfile="before",
                tofile="after",
                lineterm="",
            )
        )
    return f"<details><summary>Text captures</summary>\n\n````{fence}\n{body}\n````\n\n</details>"


def build_pr_block(
    *,
    pr: int,
    base_sha: str,
    head_sha: str,
    repo_slug: str,
    commands: list[str],
    mode: str,
    timestamp: str,
    before_text: str,
    after_text: str,
    stills: bool = True,
) -> str:
    """The PR body block inserted between the pr-demo markers."""
    before_url = asset_url(repo_slug, pr, head_sha[:7], "before.gif")
    after_url = asset_url(repo_slug, pr, head_sha[:7], "after.gif")
    commands_list = "\n".join(f"- `{command}`" for command in commands)
    stills_line = ""
    if stills:
        stills_line = (
            "\nStill frames: "
            f"[before]({asset_url(repo_slug, pr, head_sha[:7], 'before.png')}), "
            f"[after]({asset_url(repo_slug, pr, head_sha[:7], 'after.png')})\n"
        )
    return (
        f"{START_MARKER} sha={head_sha} -->\n"
        "## Before / after demo (pr-demo)\n\n"
        "| Before | After |\n"
        "| --- | --- |\n"
        f"| ![before demo]({before_url}) | ![after demo]({after_url}) |\n"
        f"{stills_line}"
        f"\nRecorded at `{head_sha[:7]}` (before: `{base_sha[:7]}`), mode: {mode}, "
        f"recorded {timestamp}.\n\n"
        "Commands:\n\n"
        f"{commands_list}\n\n"
        f"{capture_section(before_text, after_text)}\n"
        f"{END_MARKER}"
    )


def apply_pr_block(body: str, block: str) -> str:
    """Replace any existing pr-demo block (any recorded sha) or append the block."""
    if PR_BLOCK_RE.search(body):
        updated = PR_BLOCK_RE.sub(block + "\n", body, count=1)
        return updated.rstrip("\n") + "\n"
    return f"{body.rstrip(chr(10))}\n\n{block}\n"


def repo_slug_from_remote(remote_url: str) -> str:
    """Parse owner/name from an origin URL; fall back to the upstream repo."""
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", remote_url)
    return match.group(1) if match else "databricks/unity-gateway"


# ---------------------------------------------------------------------------
# Git plumbing for the orphan assets branch
# ---------------------------------------------------------------------------


def assets_parent() -> str | None:
    """Current tip of origin/pr-demo-assets, if the branch exists."""
    result = run(["git", "ls-remote", "origin", f"refs/heads/{ASSETS_BRANCH}"], check=False)
    output = result.stdout.strip()
    return output.split()[0] if output else None


def commit_assets(out_dir: Path, head_sha: str, pr: int) -> str:
    """Build a commit holding the gifs (and PNGs) at <pr>/<short-sha>/ via plumbing."""
    prefix = f"{pr}/{head_sha[:7]}"
    blobs = []
    for name in (*GIF_NAMES, *PNG_NAMES):
        path = out_dir / name
        if not path.exists():
            if name in GIF_NAMES:
                raise SystemExit(f"missing {path}; run scripts/pr_demo/record.py first")
            continue  # PNG stills are optional
        blobs.append((name, git("hash-object", "-w", str(path))))

    parent = assets_parent()
    with tempfile.TemporaryDirectory(prefix="pr-demo-index-") as tmp:
        index = str(Path(tmp) / "index")
        env = {**os.environ, "GIT_INDEX_FILE": index}
        run(["git", "read-tree", *(["--empty"] if not parent else [parent])], env=env)
        for name, blob in blobs:
            run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{blob},{prefix}/{name}",
                ],
                check=True,
                env=env,
            )
        tree = run(["git", "write-tree"], check=True, env=env).stdout.strip()
    message = f"pr-demo assets for PR {pr} (head {head_sha[:7]})"
    parent_args = ["-p", parent] if parent else []
    return git("commit-tree", tree, *parent_args, "-m", message)


def push_assets(commit: str) -> tuple[bool, str]:
    result = run(
        ["git", "push", "origin", f"{commit}:refs/heads/{ASSETS_BRANCH}"],
        check=False,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def manual_upload_instructions(pr: int, head_sha: str, out_dir: Path) -> str:
    return (
        "Could not push to the pr-demo-assets branch (fork without push access?). "
        "Upload the gifs manually, then re-run this command:\n"
        f"  1. Open https://github.com/databricks/unity-gateway/tree/{ASSETS_BRANCH}\n"
        "  2. Add the files via the GitHub web UI (drag and drop works), creating "
        "the branch when prompted:\n"
        f"       {head_sha[:7]}/before.gif from {out_dir / 'before.gif'}\n"
        f"       {head_sha[:7]}/after.gif from {out_dir / 'after.gif'}\n"
        f"       {head_sha[:7]}/before.png from {out_dir / 'before.png'}\n"
        f"       {head_sha[:7]}/after.png from {out_dir / 'after.png'}\n"
        f"  3. Re-run publish.py --out {out_dir} --pr {pr}"
    )


# ---------------------------------------------------------------------------
# PR updates
# ---------------------------------------------------------------------------


def load_manifest(out_dir: Path) -> dict:
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"missing {manifest_path}; run scripts/pr_demo/record.py first")
    return json.loads(manifest_path.read_text())


def classify_skip(paths: list[str]) -> tuple[bool, list[str]]:
    """Only a nonempty diff entirely of test files and YAML needs no demo."""
    blockers = []
    for path in paths:
        name = Path(path).name
        is_test = (
            path.startswith("tests/")
            or name == "conftest.py"
            or any(fnmatch.fnmatchcase(name, pattern) for pattern in ("test_*.py", "*_test.py"))
        )
        if not is_test and not name.endswith((".yml", ".yaml")):
            blockers.append(path)
    return bool(paths) and not blockers, blockers


def branch_changes() -> list[str]:
    base = git("merge-base", "origin/main", "HEAD")
    # Disabling rename detection lists both old and new paths, preventing a
    # source-to-test rename from hiding the source deletion.
    git_path = shutil.which("git")
    if not git_path:
        raise SystemExit("git was not found on PATH")
    result = run([git_path, "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD"])
    return [path for path in result.stdout.split("\0") if path]


def check_skip() -> tuple[bool, list[str]]:
    paths = branch_changes()
    skip, blockers = classify_skip(paths)
    if skip:
        print("skip: all committed changes are tests or YAML files")
    elif not paths:
        print("record: the branch has no committed changes")
    else:
        print("record: non-test, non-YAML changes require a demo:")
        for path in blockers:
            print(f"  {path}")
    return skip, blockers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="record.py output directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-skip", action="store_true", help="classify committed branch changes")
    mode.add_argument(
        "--mark-no-demo-needed",
        action="store_true",
        help="label a qualifying PR without editing its body",
    )
    parser.add_argument("--pr", type=int, help="pull request number (default: gh pr view)")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the new PR body block without network writes"
    )
    args = parser.parse_args(argv)

    if args.check_skip:
        check_skip()
        return 0
    if args.mark_no_demo_needed:
        if not args.pr:
            parser.error("--mark-no-demo-needed requires --pr")
        skip, _ = check_skip()
        if not skip:
            return 1
        try:
            gh("pr", "edit", str(args.pr), "--add-label", "no-pr-demo-needed")
        except subprocess.CalledProcessError as error:
            print(
                f"warning: could not add the no-pr-demo-needed label ({error.stderr.strip()})",
                file=sys.stderr,
            )
            return 1
        print(f"PR #{args.pr} labeled no-pr-demo-needed; body unchanged")
        return 0
    if not args.out:
        parser.error("--out is required when publishing")

    out_dir = Path(args.out).resolve()
    manifest = load_manifest(out_dir)
    base_sha = str(manifest["base_sha"])
    head_sha = str(manifest["head_sha"])
    commands = [str(command) for command in manifest.get("commands", [])]
    mode = str(manifest.get("mode", "offline"))
    timestamp = str(manifest.get("timestamp", "unknown"))
    redact_host = bool(manifest.get("redact_host"))

    captures: dict[str, str] = {}
    remaining: list[tuple[str, str]] = []
    for side in ("before", "after"):
        captures[side], side_remaining = record.sanitize_capture(
            (out_dir / f"{side}.txt").read_text(), redact_host=redact_host
        )
        remaining.extend(side_remaining)
    if remaining:
        listing = "\n".join(f"  - {kind}: {value}" for kind, value in remaining)
        raise SystemExit(
            "text captures look like they contain secrets; not publishing:\n"
            f"{listing}\nRe-run record.py with --redact-host for workspace-hostname matches."
        )

    record.check_commands_for_secrets(commands, redact_host=False)
    repo_slug = repo_slug_from_remote(git("remote", "get-url", "origin"))

    if args.pr:
        pr = args.pr
    elif args.dry_run:
        pr = 0
    else:
        pr = int(json.loads(gh("pr", "view", "--json", "number,body,headRefOid").stdout)["number"])

    block = build_pr_block(
        pr=pr,
        base_sha=base_sha,
        head_sha=head_sha,
        repo_slug=repo_slug,
        commands=commands,
        mode=mode,
        timestamp=timestamp,
        before_text=captures["before"],
        after_text=captures["after"],
        stills=(out_dir / "before.png").exists() and (out_dir / "after.png").exists(),
    )

    if args.dry_run:
        print(f"dry run: would push gifs to {ASSETS_BRANCH} at {pr or '<PR>'}/{head_sha[:7]}/")
        print("dry run: PR body block:\n")
        print(block)
        return 0

    if not args.pr:
        head_oid = json.loads(gh("pr", "view", str(pr), "--json", "headRefOid").stdout)[
            "headRefOid"
        ]
        if head_oid != head_sha:
            print(
                f"warning: PR head is {head_oid[:7]} but the recordings were made at "
                f"{head_sha[:7]}; re-run record.py if this push was unexpected",
                file=sys.stderr,
            )

    commit = commit_assets(out_dir, head_sha, pr)
    print(f"built assets commit {commit[:7]}")
    ok, error = push_assets(commit)
    if not ok:
        print(manual_upload_instructions(pr, head_sha, out_dir), file=sys.stderr)
        print(f"git push failed: {error}", file=sys.stderr)

    current_body = json.loads(gh("pr", "view", str(pr), "--json", "body").stdout)["body"]
    new_body = apply_pr_block(current_body, block)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(new_body)
        body_file = handle.name
    try:
        gh("pr", "edit", str(pr), "--body-file", body_file)
    finally:
        Path(body_file).unlink()

    try:
        gh("pr", "edit", str(pr), "--add-label", LABEL)
    except subprocess.CalledProcessError as error:
        print(
            f"warning: could not add the {LABEL} label ({error.stderr.strip()}); "
            f"a maintainer should run `gh label create {LABEL}` once",
            file=sys.stderr,
        )
    print(f"published: {asset_url(repo_slug, pr, head_sha[:7], 'before.gif')}")
    print(f"published: {asset_url(repo_slug, pr, head_sha[:7], 'after.gif')}")
    print(f"PR #{pr} body updated and labeled {LABEL} (when the label exists)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
