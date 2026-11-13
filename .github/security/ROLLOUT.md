# Trusted security scan rollout

The scan producer and the consumer gate must be deployed in order. There is no
bootstrap pass when the trusted producer is missing.

1. Merge the prerequisite that installs `security-scan.yml`, the reusable
   `security-gate.yml`, scanner scripts/rules, and CI's `CI #<PR>` run name.
   Existing CI does not call the new gate in that prerequisite. GitHub only
   starts `workflow_run` workflows after their definitions exist on `main`.
2. Rebase/update a PR so its CI has the new run name. Verify that requesting CI
   starts `Security Scan for CI run <id>` with event `workflow_run`, that its
   resolver selects the correct PR/head repository/SHA, and that its policy
   checkout uses the workflow's `main` commit. Exercise an untrusted-author scan.
3. Merge the enforcement change: CI/publisher consumers, the authorized
   `/integration-test` consumer, and `required-security.yml`.
4. An organization administrator must configure the controls below and verify
   them before describing this as enforced against malicious fork workflows.

## Required workflow rule

Create an active organization ruleset targeting `databricks/unity-gateway`'s
default branch, with **Require workflows to pass before merging** selecting:

- Source repository: `databricks/unity-gateway`
- Workflow: `.github/workflows/required-security.yml`
- Source ref: `refs/heads/main` (protected)

Keep the existing test/e2e checks and merge queue. The verifier supports
`pull_request` and `merge_group`; PRs require a trusted scan, while merge groups
reuse the PR approval/scan policy. Push a new commit or reopen existing PRs after
enabling the rule. Use an actual required-workflow rule, not another check name:
check-name rules do not bind a check to a trusted workflow or event.

Without this organization rule, a PR can remove its ordinary CI wait job. The
default-branch `/integration-test` path still checks scan provenance and refuses
PR-produced substitutes, but normal PR execution/merge is not fully enforced.

## Protected runner group

An administrator must inspect `databricks-protected-runner-group` and restrict
workflow access to approved workflows pinned to trusted refs, or remove this
public repository's access and provide a suitably restricted group. For the
new scan path the required direct workflow definitions are:

```
databricks/unity-gateway/.github/workflows/security-scan.yml@refs/heads/main
databricks/unity-gateway/.github/workflows/security-gate.yml@refs/heads/main
```

The existing authorized comment path also uses protected jobs directly in
`fork-integration.yml@refs/heads/main`. Preserve other approved consumers only
after auditing their source refs; do not replace a shared organization's
allowlist with these entries blindly. A job-level `if` or runner-selection
expression in a PR-controlled workflow is not a runner access restriction.

Existing protected consumers in `integration.yml`, `user-journey-test-required.yml`,
`ug-review.yml` and `polly-review.yml` need an admin migration review too. Their
PR-triggered definitions may currently run at PR refs and would be denied by a
main-only restriction. Move those protected jobs to trusted default-branch paths
or narrowly approved reusable workflows before switching that policy on.

The current credentials have repository push/triage access, not admin access.
The org runner-groups endpoint returned HTTP 403 requiring org admin or the
runner-group fine-grained permission. No organization settings were changed or
verified. Record the approved group configuration and required-workflow rule here
after an administrator applies them.

## Validation and operating limits

- The trusted producer handles only PR CI requests. It reads no PR code on its
  protected resolver runner and scans PR files statically on a separate hosted
  runner with read-only permissions and no repository secrets.
- The consumer matches CI path/event/PR/head and accepts only a `workflow_run`
  scan for that CI run ID. A same-named `pull_request` check cannot satisfy it.
- Re-running CI does not emit another `workflow_run: requested` event. It reuses
  the original scan; re-run the trusted scan to rescan. A new commit or reopening
  the PR creates a new CI request. Labels are read from GitHub by the resolver.
- Public consumers poll once per minute, cache the selected CI request and limit
  scan queries to runs created since that request. Public API rate limits still
  block tests, with a message to retry after the reset time.
- A successful static scan is not proof arbitrary code is safe. Human
  authorization remains required by the credentialed integration path; #1638
  separately makes that authorization member-only.

References: [workflow_run event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run),
[ruleset workflows](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/troubleshooting-rules#troubleshooting-ruleset-workflows),
[runner workflow access](https://docs.github.com/en/enterprise-cloud@latest/actions/how-tos/manage-runners/self-hosted-runners/manage-access#changing-which-workflows-can-access-a-runner-group).
