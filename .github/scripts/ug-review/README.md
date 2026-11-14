# UG review

Add either `ug-review` or `code-review` to an in-repository PR, mark an
in-repository draft PR ready for review, or have an organization member or owner
post exactly `/ug-review` on any PR, to run
[Open Code Review](https://github.com/alibaba/open-code-review) with the UG rubric.
Label and ready-for-review triggers do not run on fork PRs, whose `pull_request`
events cannot access the gateway secret; use the member comment instead. Findings
become inline review comments. OCR always posts a summary comment, so the workflow deletes it when every
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
default-branch workflow, and the action checks out the default branch. Label and
ready-for-review events use the PR's workflow definition, and the action checks out
the PR merge ref; they are restricted to in-repository branches. OCR reads the
diff through Git and never installs dependencies or executes PR code.

## Verification

### Human review themes

The shared rubric includes feedback from the 121 open PRs created in the 14 days
ending November 14, 2026 at 03:25:55 UTC, plus the explicitly requested closed PR 1630.
Issue comments, review bodies, and inline
threads, including resolved threads, were inspected with `github-actions[bot]`
excluded. Command comments and author replies describing bot fixes were not used
as new review policy. Non-bot accounts can also post automated reviews; the source
account alone does not establish manual authorship.

| Feedback | Rubric check | Source |
| --- | --- | --- |
| Prefer native mechanisms to another language proxy | Compare startup/debugging costs and concrete alternatives | [1589](https://github.com/databricks/unity-gateway/pull/1589) |
| Document metrics binding and reuse metric/operation constants | Metrics package, registries, and `AGENTS.md` guidance | [1630](https://github.com/databricks/unity-gateway/pull/1630), [1631](https://github.com/databricks/unity-gateway/pull/1631#discussion_r4559219089), [1632](https://github.com/databricks/unity-gateway/pull/1632), [1633](https://github.com/databricks/unity-gateway/pull/1633), [1634](https://github.com/databricks/unity-gateway/pull/1634) |
| Prove telemetry delivery and downstream attribution | Real recorder request/response and explicit usage-data limits | [1631](https://github.com/databricks/unity-gateway/pull/1631#discussion_r4559234355), [1626](https://github.com/databricks/unity-gateway/pull/1626) |
| Verify native UI behavior | Readable model-selector entries and a completed response with the expected result | [1592](https://github.com/databricks/unity-gateway/pull/1592) |
| Reuse managed headers and server naming rules | Existing header/config paths and compatible server canonical names | [1406](https://github.com/databricks/unity-gateway/pull/1406#discussion_r4543353954), [1396](https://github.com/databricks/unity-gateway/pull/1396#discussion_r4447592947), [1525](https://github.com/databricks/unity-gateway/pull/1525) |
| Keep one source for ownership policy and shared pins | Reuse authoritative definitions without broad incidental refactors | [1552](https://github.com/databricks/unity-gateway/pull/1552) |
| Consider latency and confusing hook UI | Before/after evidence for material waits and visible tradeoffs | [1629](https://github.com/databricks/unity-gateway/pull/1629), [1542](https://github.com/databricks/unity-gateway/pull/1542#issuecomment-6788468371) |
| Use approved registries, narrow scopes, and trusted CI policy | Supply-chain and workflow trust boundaries | [1586](https://github.com/databricks/unity-gateway/pull/1586#discussion_r4540831674), [1412](https://github.com/databricks/unity-gateway/pull/1412#discussion_r4519453184), [1629](https://github.com/databricks/unity-gateway/pull/1629) |

Both review workflows consume the same trusted default-branch rubric. Polly also
applies it to its Approach and Tests sections. Prompt changes take effect after
merge; a review of this PR still uses the current default-branch policy.
These are review priorities with concrete finding requirements, not a guarantee
that a model catches every future issue.

### Component and live checks

`uv run pytest tests/test_ug_review_bot.py` covers the token exchange and workflow
contract. These component checks do not establish gateway inference, CLI
installation through the proxy, or live comment posting.

After this workflow and its auth helper are merged to the default branch, test
against an open PR by adding either review label or posting `/ug-review`. Watch
the **UG Review** run for successful gateway authentication and a complete review,
then verify that an actionable finding appears inline on the changed lines.
Use a small deliberate logic bug on a disposable PR to test positive findings,
and remove it afterward. Remove and re-add a label, or post a new comment, to rerun.
Marking a draft PR ready for review requests one review; a PR opened as ready
does not. Existing labels, label removal, label edits, and ordinary pushes do not
request another review.

## Polly review

The same triggers (`ug-review` or `code-review` label on an in-repo PR, an in-repo
draft marked ready for review, or a member `/ug-review` comment) also start
**Polly AI Review** (`.github/workflows/polly-review.yml`), ported from Omnigent's `polly-review.yml`. It runs Omnigent's `examples/polly` reviewer from
a pinned Omnigent commit against a frozen PR diff and posts one summary comment, skipping a
head commit that already has a Polly review. The runner, service principal, and gateway
token are the same as above; Omnigent's providers point at the gateway's
`/ai-gateway/anthropic` and `/ai-gateway/codex/v1` routes. Omnigent's Python
dependencies and the `@anthropic-ai/claude-code` and `@openai/codex` CLIs (pinned to the
`integration.yml` versions) install through the Databricks JFrog proxies, authenticated with
GitHub OIDC. Polly reads the trusted default-branch `AGENTS.md` and UG rubric as policy and
treats the PR as untrusted data. Set the repository variables `UG_POLLY_HARNESS` and
`UG_POLLY_MODEL` to override the defaults (`codex`, `system.ai.gpt-6-1-sol`, the model main's old reviewer used through `UG_CI_REVIEW_MODEL`).

`.github/scripts/polly-review/summary.py` is adapted from Omnigent (Apache-2.0).
`tests/test_polly_review.py` covers the workflow contract and the summary helper, not live
inference. Disposable smoke runs on the protected runner verified the apt tmux install,
the frozen-lockfile Omnigent install through JFrog, and a posted review.
