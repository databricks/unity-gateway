---
name: pr-demo
description: Record BEFORE (merge-base with origin/main) and AFTER (PR head) terminal recordings of the Unity Gateway CLI with a lightweight built-in recorder, publish the gifs to the current pull request description, and add the pr-demo-recorded label.
---

# pr-demo: before/after terminal recordings for a pull request

Record the same `ug` commands against the merge-base with `origin/main` (BEFORE)
and the PR head (AFTER), show both gifs in the PR description, and label the PR
`pr-demo-recorded`. The recorder is built in: commands run under a real pty
(macOS/Linux; Windows falls back to a non-pty pipe capture with `FORCE_COLOR=1`,
so interactive flows are macOS/Linux only), the output is replayed through
`pyte`, and frames are rendered to an animated GIF with Pillow. Recordings run
in disposable virtualenvs built from the two commits, inside a sandboxed HOME,
so nothing touches the developer's real configuration. Gifs are pushed to the
orphan branch `pr-demo-assets` in `databricks/unity-gateway` and linked from the
PR body.

## Prerequisites

- `uv` (the repo's normal toolchain) — the two recorder-only dependencies
  (`pyte`, `Pillow`) are pulled in per command with `--with`; they are NOT
  project dependencies and never touch the lock file.
- `gh` (GitHub CLI) authenticated against `databricks/unity-gateway`.

Tested with Claude Code and Codex 0.167+.

## Workflow

Work from the repository root:

```bash
ROOT=$(git rev-parse --show-toplevel)
```

1. **Check whether a demo is needed.** Run:

   ```bash
   uv run python "$ROOT/scripts/pr_demo/publish.py" --check-skip
   ```

   A nonempty committed diff from the merge-base with `origin/main` that contains
   only files under `tests/`, files named `test_*.py`, `*_test.py`, or
   `conftest.py`, and `.yml`/`.yaml` files needs no demo. This includes YAML
   anywhere, even under `src/`. Renames and deletions are included; uncommitted
   changes are not. If it prints `skip`, obtain the PR number with
   `gh pr view --json number`, then run:

   ```bash
   uv run python "$ROOT/scripts/pr_demo/publish.py" --mark-no-demo-needed --pr <number>
   ```

   This records the skip in the PR body as a single plain
   `NO_PR_DEMO_NEEDED_RATIONALE=only test files and .yml/.yaml files changed (<n> files)`
   line at column 0 (an existing line is replaced in place; running it twice is
   idempotent and the rest of the body is preserved) and adds the
   `no-pr-demo-needed` label. Report the result and stop. On a body or label
   failure, report the warning and stop. An empty or mixed diff prints `record`.
2. **Verify the branch is pushed and has an open PR.** Run
   `gh pr view --json number,headRefOid` and check that the PR head matches the
   branch. If no PR exists, report that and stop.
3. **Select commands: use supplied ones, or trace the diff.** If the invocation
   that started this skill includes `ug ...` commands (for example
   `/pr-demo ug configure --foo`, or several separated by `;` or newlines), use
   them as given and skip the discovery below. The identical-output check in the
   recording step still applies; if a supplied command shows no change, report
   that instead of silently substituting other commands.

   Otherwise run `git diff "$(git merge-base origin/main HEAD)"..HEAD` and trace
   each changed function or string under `src/ucode/` to a command:

   - Grep for its callers up to the Typer command in `src/ucode/cli.py` (the
     `@app.command` / `app.add_typer` registration) that reaches it.
   - Prefer arguments that reach the changed line without a workspace:
     validation errors, flag conflicts, `--help` text, or early returns.
   - Verify each candidate locally, offline, in a throwaway home:

     ```bash
     HOME=$(mktemp -d) UG_SKIP_OS_SETTINGS=1 uv run ug <args>
     ```

     It must exit within a few seconds with no prompt. Use `--workspace` only
     if no offline path reaches the change. Keep 2–4 verified commands.

   Worked example: a changed `RuntimeError` message in configure's flag
   validation is reached with
   `ug configure --model-location main.default --provider main.default.anthropic`
   (it prints `ERROR --model-location and --provider cannot be combined.` and
   exits in a few seconds, offline).
4. **Record.** For example:

   ```bash
   uv run --with pyte --with pillow python "$ROOT/scripts/pr_demo/record.py" \
     --command "ug --help" \
     --command "ug doctor --help"
   ```

   Useful flags:
   - `--commands-file <file>` (one command per line) instead of repeated
     `--command`.
   - `--base <sha>` / `--head <sha>` to override the merge-base/HEAD defaults.
   - `--workspace --profile <name>` opts in to workspace mode when offline
     commands cannot show the change. The recorder runs a hidden
     `ug configure --profile <name> --use-pat` setup inside the sandbox per
     version, using the developer's real `~/.databrickscfg`. `--redact-host`
     redacts text only, not images; a host in the recording blocks the run.
   - `--rows` / `--cols` to resize the terminal (default 30x100).

   Set `OUT` to the printed `Output directory:` path for publishing. Each run
   creates a new temporary directory outside the checkout, which the OS may
   clean up; pass `--out <directory>` to choose another location. Frames retain
   the terminal width and share a cropped height across both recordings.

   Pass working-tree and unpushed-HEAD warnings on to the developer. Per-commit
   virtualenvs are cached under `~/.cache/ug-pr-demo/<sha>`.

   If record.py exits with code 3 (`BEFORE and AFTER output are identical`),
   the picked commands show no change: choose different commands and retry,
   at most 3 recording attempts in total. Never pass `--allow-identical` to
   bypass this check. If every attempt is identical, go to step 6 instead of
   publishing an unchanged demo.
5. **Publish.** Run:

   ```bash
   uv run python "$ROOT/scripts/pr_demo/publish.py" --out "$OUT"
   ```

   Use `--dry-run` to preview without a network write. A secret scan hit blocks
   recording or publishing; change the offending command and retry, or report the
   failure and stop. Never bypass the scan. The scanner is pattern-based, not a
   guarantee that arbitrary sensitive data is absent. The publisher pushes the
   gifs and PNGs to `pr-demo-assets` at `<pr>/<short-sha>/{before,after}.{gif,png}`,
   replaces the PR body block between `<!-- pr-demo:start ... -->` and
   `<!-- pr-demo:end -->`, removes any stale `NO_PR_DEMO_NEEDED_RATIONALE=` line
   and the `no-pr-demo-needed` label (the PR now has a demo), and adds
   `pr-demo-recorded`.
6. **No visible change: skip with a reason.** If the change has no user-visible
   CLI output (for example an internal refactor), or three recording attempts
   all produced identical output, mark the PR with an agent-judged reason:

   ```bash
   uv run python "$ROOT/scripts/pr_demo/publish.py" --mark-no-demo-needed \
     --pr <number> \
     --reason "<why no demo can show this change>" \
     --tried "<command tried>" --tried "<another command>"
   ```

   `--reason` writes a short block between the usual `<!-- pr-demo:start ... -->`
   and `<!-- pr-demo:end -->` markers (replacing any existing block idempotently)
   plus the same `NO_PR_DEMO_NEEDED_RATIONALE=<reason>` line outside the block,
   then adds `no-pr-demo-needed`. List the commands you attempted with the
   repeatable `--tried`. The reason and tried commands go through the secret
   scan; a hit blocks the update. Without `--reason`, the classifier must say
   `skip` (step 1) or the command refuses.
7. **Report the result** (image links and PR link, or the skip rationale).

## Failure handling

- If pushing to `pr-demo-assets` fails (fork without push access), publish.py
  prints manual upload instructions and still updates the PR body and label; pass
  the instructions on to the developer.
- If adding the label fails, a maintainer should run
  `gh label create pr-demo-recorded` once; tell the developer.
- The label is informational and is never removed on new pushes; re-running
  publish.py after new commits replaces the PR body block and pushes new gifs.
