# UG review

Add either `ug-review` or `code-review` to an in-repository PR, or have an
organization member or owner post exactly `/ug-review` on any PR, to run
[Open Code Review](https://github.com/alibaba/open-code-review) with the UG rubric.
Label triggers do not run on fork PRs, whose `pull_request` events cannot access
the gateway secret; use the member comment instead. Findings become inline review
comments. OCR always posts a summary comment, so the workflow deletes it when every
finding was posted inline; Polly posts the overall review. The summary stays only
when it holds findings GitHub would not accept inline.

The job retains the existing protected runner, workspace, service principal, and
`UG_CLI_REVIEW_BOT_SP_CLIENT_SECRET`. `auth.py` uses the same OAuth
`client_credentials` exchange with `scope=all-apis`, masks the result, and exposes
it only as a same-job step output. No Anthropic key or developer profile is needed.
OCR sends it as a bearer token to the native gateway route `/ai-gateway/anthropic`
with the `system.ai.claude-opus-5-5` service and adaptive thinking. The service
principal needs access to that service. The old `UG_CI_REVIEW_MODEL` variable is
no longer used.

## Action and CLI

The workflow is adapted from Omnigent's `open-code-review.yml`. It uses the
unmodified action at a pinned commit and the pinned `1.12.0` CLI. The action
installs the CLI with `npm install -g`; npm uses the existing `db-npm` proxy at
Databricks' JFrog, authenticated through the same GitHub OIDC setup used by the
repository's terminal-bench workflow. No registry secret is stored in GitHub. The
owner-only `.npmrc` lives under `runner.temp` and is removed even when review fails.
TLS verification stays enabled.

`.github/open-code-review/rules.json` adds workflow and Python test files, which
OCR excludes by default, to the reviewed set. The trusted repository `AGENTS.md`
and UG rubric are passed as the review background. Raw OCR artifacts are not
uploaded; a final step reports coverage in the job summary and fails the job when
the review is incomplete or does not match the requested head commit.

## Trust boundary

The job fetches `auth.py`, `AGENTS.md`, and the rubric from the default branch
before the action re-checks out the workspace. Comment events run the
default-branch workflow, and the action checks out the default branch. Label
events use the PR's workflow definition, and the action checks out the PR merge
ref; they are restricted to in-repository branches. OCR reads the diff through Git
and never installs dependencies or executes PR code.

## Verification

`uv run pytest tests/test_ug_review_bot.py` covers the token exchange and workflow
contract. These component checks do not establish gateway inference, CLI
installation through the proxy, or live comment posting.

After this workflow and its auth helper are merged to the default branch, test
against an open PR by adding either review label or posting `/ug-review`. Watch
the **UG Review** run for successful gateway authentication and a complete review,
then verify that an actionable finding appears inline on the changed lines.
Use a small deliberate logic bug on a disposable PR to test positive findings,
and remove it afterward. Remove and re-add a label, or post a new comment, to rerun.
Existing labels, label removal, label edits, and ordinary pushes do not request
another review.
