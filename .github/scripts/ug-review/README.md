# UG review

Add either `ug-review` or `code-review` to an in-repository PR, or have an
organization member or owner post exactly `/ug-review` on any PR, to run the
official Claude Code action and its code-review plugin. Label triggers do not run
on fork PRs, whose `pull_request` events cannot access the gateway secret; use the
member comment instead. Findings become inline review comments; a clean review
gets one short PR conversation comment.

The job retains the existing protected runner, workspace, service principal, and
`UG_CLI_REVIEW_BOT_SP_CLIENT_SECRET`. `auth.py` uses the same OAuth
`client_credentials` exchange with `scope=all-apis`, masks the result, and exposes
it only as a same-job step output. No Anthropic key or developer profile is needed.
The native gateway route is `/ai-gateway/anthropic`.

| Claude alias | Gateway service |
| --- | --- |
| Haiku | `system.ai.claude-haiku-5-5` |
| Sonnet and root reviewer | `system.ai.sonnet-5-5` |
| Opus | `system.ai.claude-opus-5-5` |

The service principal needs access to all three services. These are explicit
service IDs, not a claim that every workspace has them. The old
`UG_CI_REVIEW_MODEL` variable is no longer used.

## Stock action and plugin

The workflow uses the unmodified official action at a pinned commit and installs
the official code-review plugin from Anthropic's marketplace. Dependabot's existing
GitHub Actions updates can bump the action pin. There is no fork or source patch.
The only custom Python code is the existing service-principal token exchange.

The plugin retains its multi-agent review and finding validation. The separate
optional `classify_inline_comments` filter is disabled because it hardcodes a
direct Anthropic API call instead of honoring the gateway configuration.

The trusted repository policy and UG rubric are appended to both the parent and
subagent system prompts. PR code is not checked out. Tool permissions allow review
reads and comments, not editing files or running PR commands.

## Verification

`uv run pytest tests/test_ug_review_bot.py` covers the token exchange and workflow
contract. The obsolete custom Responses request, diff budgeting, JSON parsing, and
summary rendering are removed rather than maintained alongside the official action.
These component checks do not establish gateway inference or live inline posting.

After this workflow and its auth helper are merged to the default branch, test
against an open PR by adding either review label or posting `/ug-review`. Watch
the **UG Review** run for successful gateway authentication and plugin execution,
then verify that an actionable finding appears inline on the changed lines.
Use a small deliberate logic bug on a disposable PR to test positive findings,
and remove it afterward. Remove and re-add a label, or post a new comment, to rerun.
Existing labels, label removal, label edits, and ordinary pushes do not request
another review.

Before merge, the focused component tests and workflow audit can run locally, but
they are not an end-to-end test. Comment triggers use the default-branch workflow;
label-triggered runs also fetch the policy and auth helper from the default branch,
so they cannot test a new helper that has not been merged yet.
