---
name: pr-demo
description: Record and publish BEFORE/AFTER Unity Gateway PR demos using actual PR code in a native environment connected to an authenticated live workspace.
---

# pr-demo: before/after terminal recordings for a pull request

Record the same `ug` commands against the merge-base with `origin/main` (BEFORE)
and the PR head (AFTER), show both gifs in the PR description, and label the PR
`pr-demo-recorded`. The built-in recorder uses a pty, pyte, and Pillow, with
per-commit virtualenvs and a sandboxed HOME. Images are public release assets in
`databricks/unity-gateway`. Cleanup deletes them after the PR closes; the text
diff stays in the PR body.

## Live demo requirement

- ALWAYS run the actual reviewed code, checked out and built at each revision,
  through the native CLI or app against an authenticated live workspace.
  NEVER use fixtures, mocks, stubs, replayed responses, or fake APIs/proxies,
  including localhost substitutes, as demo proof. Isolated HOME or tmpfs policy
  paths are allowed only with real code, auth, config, native runtime, and live
  service requests. Native proxies must forward to the real service.
- Show setup and launch commands, workspace selection, effective config version
  (or explicitly unset), and relevant flags. Show a real operation and its
  outcome through the changed path. Help/version output, offline errors, and
  unit tests are insufficient. Hidden preconfiguration is not proof.
- Preserve actual elapsed timing, including startup, waits, inactivity, and
  shutdown. Never accelerate playback, trim idle time, or splice sessions into
  an apparent continuous success.
- Keep secrets out of public artifacts and never bypass scanning. Show a public
  workspace URL only when explicitly permitted and allowed by the scan;
  otherwise use a verified workspace/profile alias or supported host redaction.
- If live access, authentication, native execution, or faithful recording is
  unavailable, report the limitation and stop without publishing or adding
  `pr-demo-recorded`. Do not ask for access, fabricate success, or mark the demo
  unnecessary because validation was blocked.

## Prerequisites

- `uv`; recorder-only dependencies `pyte` and `Pillow` use `--with` and do not
  change project dependencies or the lock file.
- `gh` authenticated against `databricks/unity-gateway`.
- An available authenticated live workspace and the required native runtime.

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

   A nonempty committed diff containing only files under `tests/`, files named
   `test_*.py`, `*_test.py`, or `conftest.py`, and `.yml`/`.yaml` files needs no
   demo. Renames and deletions count; uncommitted changes do not. If it prints
   `skip`, get the PR number with `gh pr view --json number`, then run:

   ```bash
   uv run python "$ROOT/scripts/pr_demo/publish.py" --mark-no-demo-needed --pr <number>
   ```

   This writes `NO_PR_DEMO_NEEDED_RATIONALE=only test files and .yml/.yaml files changed (<n> files)`
   at column 0 in the PR body and adds `no-pr-demo-needed`, preserving the rest
   of the body. Report the result or failure and stop. Empty or mixed diffs record.
2. **Verify the revisions.** Run `gh pr view --json number,headRefOid` and check
   that the open PR head matches HEAD. Verify the recorder builds BEFORE from
   the merge-base and AFTER from that exact head, including the changed files.
   Stop if uncommitted or unpushed changes would be omitted. Bind launches to
   those builds rather than an unrelated installed `ug`.
3. **Select live commands.** Use supplied commands if they meet the live demo
   requirement; otherwise explain why they cannot validate the change and stop.
   Do not silently substitute commands. Without supplied commands, inspect
   `git diff "$(git merge-base origin/main HEAD)"..HEAD` and trace changed code
   to its CLI entry point in `src/ucode/cli.py`. Verify commands that reach the
   change with a real workspace operation. For agent changes, launch the actual
   agent and complete a real request. Keep inputs and config comparable across
   revisions except for the intended change.
4. **Record.** Workspace mode is mandatory in this skill. Put the verified
   visible setup and launch commands in a file, one command per line, then run:

   ```bash
   uv run --with pyte --with pillow python "$ROOT/scripts/pr_demo/record.py" \
     --workspace --profile <existing-live-profile> --commands-file <file>
   ```

   Repeated `--command` flags are also supported. Check these recorder limits
   before proceeding:

   - Workspace mode runs hidden `ug configure --profile <name> --use-pat` setup.
     Do not let it mask the behavior under review or replace visible setup.
   - The sandbox isolates HOME, sets `UG_SKIP_OS_SETTINGS=1`, and drops inherited
     variables. Apply required config/version settings explicitly to the launch.
     Stop if the changed behavior needs native capabilities the sandbox disables.
   - Commands are captured separately; the Windows pipe fallback is noninteractive.
     Verify playback against actual elapsed time, including silence before and
     after output. Stop if the recorder cannot preserve the required evidence.

   `--base` / `--head` override revisions; `--rows` / `--cols` resize the terminal.
   `--redact-host` redacts text only, not images; a host in an image blocks the
   run. Set `OUT` to the printed `Output directory:` path. Output defaults to a
   fresh temporary directory outside the checkout; `--out` overrides it.

   Exit code 3 means all BEFORE/AFTER output is identical. Retry with commands
   that show the change, at most 3 valid live attempts total. For supplied
   commands, report identical output instead of substituting others. Never use
   `--allow-identical`. If there is no visible change, go to step 6.
5. **Review and publish.** Watch both recordings and inspect their transcripts
   and `manifest.json`. Confirm `mode` is `workspace`, the SHAs match step 2,
   and the evidence meets the live demo requirement. Preview, then publish:

   ```bash
   uv run python "$ROOT/scripts/pr_demo/publish.py" --out "$OUT" --dry-run
   uv run python "$ROOT/scripts/pr_demo/publish.py" --out "$OUT"
   ```

   A secret scan hit blocks recording or publishing. Change the offending command
   and retry, or report the failure and stop. The pattern-based scanner cannot
   guarantee arbitrary sensitive data is absent; inspect the artifacts too.
   The publisher uploads GIFs/PNGs to the `pr-demo-assets` prerelease, replaces
   only the `<!-- pr-demo:start ... -->` / `<!-- pr-demo:end -->` body block,
   removes stale skip rationale and `no-pr-demo-needed`, and adds `pr-demo-recorded`.
6. **No visible change: skip with a reason.** Use this for a change with no
   user-visible CLI output or three identical valid live attempts:

   ```bash
   uv run python "$ROOT/scripts/pr_demo/publish.py" --mark-no-demo-needed \
     --pr <number> --reason "<why no demo can show this change>" \
     --tried "<command tried>" --tried "<another command>"
   ```

   This writes the reason and tried commands inside the usual body markers,
   plus a top-level `NO_PR_DEMO_NEEDED_RATIONALE=<reason>` line, and adds
   `no-pr-demo-needed`. The reason and commands pass through the secret scan.
   Without `--reason`, the classifier must say `skip` or the command refuses.
7. **Report the result.** Include image links and the PR link, or the skip
   rationale. If blocked, state what could not be validated and that no demo
   was published.

## Failure handling

- If release upload fails, publish.py prints manual upload instructions and still
  updates the PR body and label; pass those instructions on to the developer.
- If adding the label fails, tell the developer a maintainer should run
  `gh label create pr-demo-recorded` once.
- The label is informational and is not removed on new pushes. Re-running
  publish.py replaces the demo block and uploads new images.
