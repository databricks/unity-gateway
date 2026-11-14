#!/usr/bin/env python3
"""Publish pr-demo recordings to a pull request.

Re-runs the secret scan, uploads images to the `pr-demo-assets` GitHub release,
edits the PR body
between the `<!-- pr-demo:start ... -->` / `<!-- pr-demo:end -->` markers, and
adds the `pr-demo-recorded` label. Publishing also removes any stale
`NO_PR_DEMO_NEEDED_RATIONALE=` line from the body and drops the
`no-pr-demo-needed` label. `--dry-run` prints the new PR body block without
any network write.

Every skip path also updates the PR body: it carries exactly one plain
`NO_PR_DEMO_NEEDED_RATIONALE=<rationale>` line (top-level, outside any code
fence) plus the `no-pr-demo-needed` label. `--mark-no-demo-needed` without
`--reason` uses an automatic rationale for test/YAML-only branches;
`--reason "<text>"` records an agent-judged skip with a human-readable block.

Usage:
    uv run python scripts/pr_demo/publish.py --out pr-demo-out [--pr 123] [--dry-run]
    uv run python scripts/pr_demo/publish.py --mark-no-demo-needed --pr 123 \
        [--reason "<text>" [--tried "<command>"] ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import record  # same directory; provides the shared secret-scan helpers

ASSETS_RELEASE = "pr-demo-assets"
LABEL = "pr-demo-recorded"
NO_DEMO_LABEL = "no-pr-demo-needed"
RATIONALE_PREFIX = "NO_PR_DEMO_NEEDED_RATIONALE="
RATIONALE_MAX_CHARS = 300
FENCE_RE = re.compile(r"^\s*(```|~~~)")
START_MARKER = "<!-- pr-demo:start"
END_MARKER = "<!-- pr-demo:end -->"
# Markers count only on their own lines, so prose that mentions them inline
# (e.g. in backticks) is never treated as the block.
PR_BLOCK_RE = re.compile(
    r"^"
    + re.escape(START_MARKER)
    + r"[^>\n]*-->[ \t]*\n.*?^"
    + re.escape(END_MARKER)
    + r"[ \t]*(?:\n|\Z)",
    re.DOTALL | re.MULTILINE,
)
GIF_NAMES = ("before.gif", "after.gif")
PNG_NAMES = ("before.png", "after.png")


def run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None):
    return subprocess.run(cmd, check=check, capture_output=True, text=True, env=env)  # noqa: TID251 - runs before ug is installed


def git(*args: str, check: bool = True) -> str:
    git_path = shutil.which("git")
    if not git_path:
        raise SystemExit("git was not found on PATH")
    return run([git_path, *args], check=check).stdout.strip()


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    gh_path = shutil.which("gh")
    if not gh_path:
        raise SystemExit("gh was not found on PATH; install the GitHub CLI first")
    return run([gh_path, *args], check=check)


# ---------------------------------------------------------------------------
# Pure helpers (covered by tests/test_pr_demo.py)
# ---------------------------------------------------------------------------


def asset_name(pr: int, short_sha: str, name: str) -> str:
    return f"pr{pr}-{short_sha}-{name}"


def asset_url(repo_slug: str, pr: int, short_sha: str, name: str) -> str:
    return (
        f"https://github.com/{repo_slug}/releases/download/{ASSETS_RELEASE}/"
        f"{asset_name(pr, short_sha, name)}"
    )


def stale_assets(assets: list[str], pr: int, short_sha: str) -> list[str]:
    prefix = f"pr{pr}-"
    current = f"{prefix}{short_sha}-"
    return [name for name in assets if name.startswith(prefix) and not name.startswith(current)]


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


def normalize_rationale(text: str) -> str:
    """One plain line: collapse whitespace, strip, and cap near the limit."""
    collapsed = " ".join(text.split())
    if len(collapsed) > RATIONALE_MAX_CHARS:
        collapsed = collapsed[: RATIONALE_MAX_CHARS - 3].rstrip() + "..."
    return collapsed


def _rationale_lines(body: str) -> tuple[list[int], list[int]]:
    """Indices of rationale lines outside and inside code fences."""
    outside: list[int] = []
    fenced: list[int] = []
    in_fence = False
    for index, line in enumerate(body.split("\n")):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if line.startswith(RATIONALE_PREFIX):
            (fenced if in_fence else outside).append(index)
    return outside, fenced


def apply_rationale_line(body: str, rationale: str) -> str:
    """Body with exactly one top-level NO_PR_DEMO_NEEDED_RATIONALE line.

    An existing top-level line is replaced in place; extra or code-fenced
    copies are removed. With none present, the line is appended at the end
    after a blank line. The rest of the body is preserved.
    """
    lines = body.split("\n")
    outside, fenced = _rationale_lines(body)
    for index in fenced:
        lines[index] = None  # type: ignore[assignment]
    if outside:
        lines[outside[0]] = f"{RATIONALE_PREFIX}{rationale}"
        for index in outside[1:]:
            lines[index] = None
    else:
        while lines and lines[-1] is not None and not lines[-1].strip():
            lines.pop()
        if lines and any(line is not None and line.strip() for line in lines):
            lines.append("")
        lines.append(f"{RATIONALE_PREFIX}{rationale}")
    result = "\n".join(line for line in lines if line is not None)
    return result if result.endswith("\n") else result + "\n"


def remove_rationale_line(body: str) -> tuple[str, bool]:
    """Drop every NO_PR_DEMO_NEEDED_RATIONALE line; report whether one existed."""
    lines = body.split("\n")
    outside, fenced = _rationale_lines(body)
    removed = set(outside) | set(fenced)
    if not removed:
        return body, False
    kept = [line for index, line in enumerate(lines) if index not in removed]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept) + "\n", True


def build_no_demo_block(*, reason: str, tried: list[str]) -> str:
    """Human-readable marker block for an agent-judged skip."""
    lines = [
        f"{START_MARKER} no-demo -->",
        "## No demo recorded (pr-demo)",
        "",
        "No before/after demo was recorded for this pull request.",
        "",
        f"Reason: {reason}",
    ]
    if tried:
        lines.extend(["", "Commands tried:", *(f"- `{command}`" for command in tried)])
    lines.extend(["", END_MARKER])
    return "\n".join(lines)


def scan_secret_text(kind: str, text: str) -> None:
    """Refuse text that matches the secret patterns, before any gh write."""
    _, remaining = record.sanitize_capture(text, redact_host=False)
    if remaining:
        listing = "\n".join(f"  - {matched_kind}: {value}" for matched_kind, value in remaining)
        raise SystemExit(f"{kind} looks like it contains secrets; not updating the PR:\n{listing}")


def current_pr_body(pr: int) -> str | None:
    """The current PR body, or None when it cannot be read."""
    try:
        return json.loads(gh("pr", "view", str(pr), "--json", "body").stdout)["body"]
    except (subprocess.CalledProcessError, KeyError, TypeError, json.JSONDecodeError, OSError):
        return None


def update_pr_body(pr: int, new_body: str) -> bool:
    """Replace the PR body from a file; report success."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(new_body)
        body_file = handle.name
    try:
        gh("pr", "edit", str(pr), "--body-file", body_file)
        return True
    except subprocess.CalledProcessError as error:
        print(
            f"warning: could not update the PR body ({error.stderr.strip()})",
            file=sys.stderr,
        )
        return False
    finally:
        Path(body_file).unlink()


def add_label(pr: int, label: str) -> bool:
    """Add a PR label; report success."""
    try:
        gh("pr", "edit", str(pr), "--add-label", label)
        return True
    except subprocess.CalledProcessError as error:
        print(
            f"warning: could not add the {label} label ({error.stderr.strip()})",
            file=sys.stderr,
        )
        return False


def remove_label(pr: int, label: str) -> bool:
    """Remove a PR label; a missing label is not a failure."""
    try:
        gh("pr", "edit", str(pr), "--remove-label", label)
        return True
    except subprocess.CalledProcessError as error:
        if "not found" in (error.stderr or "").lower():
            return False
        print(
            f"warning: could not remove the {label} label ({error.stderr.strip()})",
            file=sys.stderr,
        )
        return False


# ---------------------------------------------------------------------------
# Release assets
# ---------------------------------------------------------------------------


def publish_assets(out_dir: Path, head_sha: str, pr: int, repo_slug: str, *, dry_run: bool) -> None:
    """Ensure the release exists, remove older assets, then upload named copies."""
    paths = []
    for name in (*GIF_NAMES, *PNG_NAMES):
        path = out_dir / name
        if not path.exists():
            if name in GIF_NAMES:
                raise SystemExit(f"missing {path}; run scripts/pr_demo/record.py first")
            continue
        paths.append(path)
    repo_args = ("--repo", repo_slug)
    if dry_run:
        print(
            f"dry run: would run gh release view {ASSETS_RELEASE} --json assets --repo {repo_slug}"
        )
    result = gh("release", "view", ASSETS_RELEASE, "--json", "assets", *repo_args, check=False)
    if result.returncode:
        if "release not found" not in (result.stderr or "").lower():
            raise RuntimeError(f"could not read release: {(result.stderr or '').strip()}")
        create_args = (
            "release",
            "create",
            ASSETS_RELEASE,
            "--prerelease",
            "--latest=false",
            "--title",
            "pr-demo assets",
            "--notes",
            "Auto-managed by scripts/pr_demo; assets are for open PRs and are deleted when PRs close.",
            "--target",
            "main",
            *repo_args,
        )
        if dry_run:
            print("dry run: would run gh " + shlex.join(create_args))
            assets = []
        else:
            created = gh(*create_args, check=False)
            # Another publisher may have created the release since our read.
            result = gh(
                "release", "view", ASSETS_RELEASE, "--json", "assets", *repo_args, check=False
            )
            if result.returncode:
                raise RuntimeError(
                    f"could not create release: {(created.stderr or result.stderr or '').strip()}"
                )
            assets = [asset["name"] for asset in json.loads(result.stdout)["assets"]]
    else:
        assets = [asset["name"] for asset in json.loads(result.stdout)["assets"]]
    for name in stale_assets(assets, pr, head_sha[:7]):
        delete_args = ("release", "delete-asset", ASSETS_RELEASE, name, "--yes", *repo_args)
        if dry_run:
            print("dry run: would run gh " + shlex.join(delete_args))
        else:
            gh(*delete_args)
    if dry_run:
        names = [asset_name(pr, head_sha[:7], path.name) for path in paths]
        print("dry run: would copy images to temporary files named " + ", ".join(names))
        print(
            "dry run: would run gh "
            + shlex.join(("release", "upload", ASSETS_RELEASE, *names, "--clobber", *repo_args))
        )
        return
    with tempfile.TemporaryDirectory(prefix="pr-demo-assets-") as tmp:
        files = []
        for path in paths:
            destination = Path(tmp) / asset_name(pr, head_sha[:7], path.name)
            shutil.copyfile(path, destination)
            files.append(str(destination))
        gh("release", "upload", ASSETS_RELEASE, *files, "--clobber", *repo_args)


def manual_upload_instructions(pr: int, head_sha: str, out_dir: Path, repo_slug: str) -> str:
    files = "\n".join(
        f"       {out_dir / name} as {asset_name(pr, head_sha[:7], name)}"
        for name in (*GIF_NAMES, *PNG_NAMES)
        if (out_dir / name).exists()
    )
    return (
        "Could not upload release assets (fork without release access?). "
        "Ask a maintainer with release access to upload them manually:\n"
        f"  1. Open https://github.com/{repo_slug}/releases/tag/{ASSETS_RELEASE} "
        "(create the prerelease first if needed).\n"
        "  2. Edit the release and drag in copies renamed as follows:\n"
        f"{files}\n"
        f"  3. Save the release; the links in PR #{pr} will then work."
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


def report_skip(paths: list[str], skip: bool, blockers: list[str]) -> None:
    if skip:
        print("skip: all committed changes are tests or YAML files")
    elif not paths:
        print("record: the branch has no committed changes")
    else:
        print("record: non-test, non-YAML changes require a demo:")
        for path in blockers:
            print(f"  {path}")


def check_skip() -> tuple[bool, list[str]]:
    paths = branch_changes()
    skip, blockers = classify_skip(paths)
    report_skip(paths, skip, blockers)
    return skip, blockers


def mark_no_demo_needed(args: argparse.Namespace) -> int:
    """Label the PR no-pr-demo-needed and record the rationale in its body."""
    if args.reason:
        scan_secret_text("reason", args.reason)
        for command in args.tried:
            scan_secret_text("tried command", command)
        rationale = normalize_rationale(args.reason)
        block: str | None = build_no_demo_block(reason=args.reason, tried=args.tried)
        note = f"reason: {rationale}"
    else:
        paths = branch_changes()
        skip, blockers = classify_skip(paths)
        report_skip(paths, skip, blockers)
        if not skip:
            return 1
        rationale = normalize_rationale(
            f"only test files and .yml/.yaml files changed ({len(paths)} files)"
        )
        block = None
        note = "classifier: only test and YAML files changed"

    if args.dry_run:
        current = current_pr_body(args.pr)
        if current is None:
            print("dry run: could not read the current PR body (no write performed)")
            print(f"dry run: rationale line: {RATIONALE_PREFIX}{rationale}")
        else:
            body = apply_rationale_line(
                apply_pr_block(current, block) if block else current, rationale
            )
            print("dry run: resulting PR body:\n")
            print(body)
        if block:
            print("\ndry run: PR body block:\n")
            print(block)
        print(f"\ndry run: would add the {NO_DEMO_LABEL} label to PR #{args.pr}")
        return 0

    current = current_pr_body(args.pr)
    body_ok = False
    if current is None:
        print(
            "warning: could not read the PR body; trying the label only",
            file=sys.stderr,
        )
    else:
        new_body = apply_rationale_line(
            apply_pr_block(current, block) if block else current, rationale
        )
        body_ok = update_pr_body(args.pr, new_body)
    label_ok = add_label(args.pr, NO_DEMO_LABEL)
    print(
        f"PR #{args.pr} marked no-pr-demo-needed ({note}); "
        f"body updated: {body_ok}, label added: {label_ok}"
    )
    return 0 if body_ok and label_ok else 1


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
        "--reason",
        help="with --mark-no-demo-needed: an agent-judged reason written into the PR body",
    )
    parser.add_argument(
        "--tried",
        action="append",
        default=[],
        help="with --reason: a command that was tried and showed no change; repeatable",
    )
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
        return mark_no_demo_needed(args)
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

    if not manifest.get("allow_identical") and captures["before"] == captures["after"]:
        raise SystemExit(
            "BEFORE and AFTER text captures are identical; the recording shows no change. "
            "Re-record with commands whose output differs between the merge-base and "
            "HEAD, or record with --allow-identical if an unchanged demo is intentional."
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
        try:
            publish_assets(out_dir, head_sha, pr, repo_slug, dry_run=True)
        except (
            subprocess.CalledProcessError,
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            print(f"warning: could not plan release upload: {error}", file=sys.stderr)
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

    try:
        publish_assets(out_dir, head_sha, pr, repo_slug, dry_run=False)
        uploaded = True
    except (
        subprocess.CalledProcessError,
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        uploaded = False
        print(manual_upload_instructions(pr, head_sha, out_dir, repo_slug), file=sys.stderr)
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"release upload failed: {detail}", file=sys.stderr)

    current_body = json.loads(gh("pr", "view", str(pr), "--json", "body").stdout)["body"]
    new_body = apply_pr_block(current_body, block)
    new_body, rationale_removed = remove_rationale_line(new_body)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(new_body)
        body_file = handle.name
    try:
        gh("pr", "edit", str(pr), "--body-file", body_file)
    finally:
        Path(body_file).unlink()
    if rationale_removed:
        print("removed the stale NO_PR_DEMO_NEEDED_RATIONALE line from the PR body")

    try:
        gh("pr", "edit", str(pr), "--add-label", LABEL)
    except subprocess.CalledProcessError as error:
        print(
            f"warning: could not add the {LABEL} label ({error.stderr.strip()}); "
            f"a maintainer should run `gh label create {LABEL}` once",
            file=sys.stderr,
        )
    remove_label(pr, NO_DEMO_LABEL)
    status = "published" if uploaded else "pending manual upload"
    print(f"{status}: {asset_url(repo_slug, pr, head_sha[:7], 'before.gif')}")
    print(f"{status}: {asset_url(repo_slug, pr, head_sha[:7], 'after.gif')}")
    print(f"PR #{pr} body updated and labeled {LABEL} (when the label exists)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
