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

1. **Verify the branch is pushed and has an open PR.** Run
   `gh pr view --json number,headRefOid` and confirm it returns the PR number and
   the head commit the developer expects. If there is no PR yet, stop and ask the
   developer to push and open one first; recordings must map to a PR.
2. **Read the diff and propose commands.** Run
   `git diff "$(git merge-base origin/main HEAD)"..HEAD` and skim the changed
   files. Propose a short list of `ug` commands whose output visibly changes —
   prefer offline commands that need no workspace (`ug --help`, a subcommand's
   `--help`, `ug --version`, an argument error) because they are deterministic;
   real-workspace commands are also allowed. Keep the list short (2-4 commands)
   so each gif stays readable.
3. **Confirm the command list with the developer** before recording. Do not pick
   commands unilaterally.
4. **Record.** Run:

   ```bash
   uv run --with pyte --with pillow python "$ROOT/scripts/pr_demo/record.py" \
     --command "ug --help" \
     --command "ug doctor --help" \
     --out "$ROOT/pr-demo-out"
   ```

   Useful flags:
   - `--commands-file <file>` (one command per line) instead of repeated
     `--command`.
   - `--base <sha>` / `--head <sha>` to override the merge-base/HEAD defaults.
   - `--workspace --profile <name>` to opt in to real-workspace mode: the
     recorder runs a hidden `ug configure --profile <name> --use-pat` setup
     inside the sandbox per version, using the developer's real
     `~/.databrickscfg`. `--redact-host` replaces workspace hostnames in the
     published text captures.
   - `--rows` / `--cols` to resize the terminal (default 30x100).

   The script warns if the working tree is dirty or HEAD is not on a remote
   branch — pass those warnings on to the developer. Per-commit virtualenvs are
   cached under `~/.cache/ug-pr-demo/<sha>`, so a rerun is fast.
5. **Show the local outputs and get explicit confirmation before publishing.**
   The outputs are `$ROOT/pr-demo-out/{before,after}.gif`, their final-frame
   `.png` stills, and the `.txt` text captures. This is a public repository:
   point the developer at the files and wait for a yes before any network write.
   If the secret scan fails, show the listed matches and stop.
6. **Publish.** Run:

   ```bash
   uv run python "$ROOT/scripts/pr_demo/publish.py" --out "$ROOT/pr-demo-out"
   ```

   Add `--dry-run` to preview the new PR body without any network write.
   publish.py pushes the gifs and PNGs to the `pr-demo-assets` orphan branch at
   `<pr>/<short-sha>/{before,after}.{gif,png}`, edits the PR body between the
   `<!-- pr-demo:start ... -->` / `<!-- pr-demo:end -->` markers (idempotent),
   and adds the `pr-demo-recorded` label.
7. **Report the resulting URLs** to the developer (the raw github.com image
   links and the PR link).

## Failure handling

- If pushing to `pr-demo-assets` fails (fork without push access), publish.py
  prints manual upload instructions and still updates the PR body and label; pass
  the instructions on to the developer.
- If adding the label fails, a maintainer should run
  `gh label create pr-demo-recorded` once; tell the developer.
- The label is informational and is never removed on new pushes; re-running
  publish.py after new commits replaces the PR body block and pushes new gifs.
