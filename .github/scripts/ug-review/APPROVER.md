# UG review approver app

The approver is a separate GitHub App identity. Merging this helper does not create
or configure the app and does not enable approvals on its own.

A repository administrator must:

1. Create a GitHub App in the `databricks` organization with **Pull requests: Read
   and write** repository permission, and install it on `databricks/unity-gateway`.
2. Set the repository variable `UG_APPROVER_APP_ID` to the app ID, and the
   repository secret `UG_APPROVER_APP_PRIVATE_KEY` to a generated private key.
3. Configure branch protection or rulesets to count the app's approval and permit
   the app to dismiss its own reviews if dismissal is restricted. Keep any required
   human approvals. Evaluate GitHub's "require approval of the most recent reviewable
   push" rule as a merge guard and verify its behavior with the app before rollout.
   GitHub's separate "dismiss stale approvals" setting also dismisses human approvals,
   so it does not meet a bot-only dismissal policy.

The workflow uses the installation token only for same-repository pull requests.
Approvals are tied to the reviewed head, and push handling dismisses only older
approvals from the installed app's bot account. API calls and workflow scheduling
are asynchronous: a push can race approval creation, and dismissal can fail. The
post-approval check attempts to withdraw raced approvals but cannot provide an atomic
merge guard. Until a suitable merge guard is verified, a maintainer must check that
an app approval matches the current head before merging; inspect and dismiss stale
approvals if the workflow fails. The installation token is revoked at job completion.

Run the helper's component tests with `node --test .github/scripts/ug-review/approver.test.cjs`.
They do not exercise the GitHub API or branch protection.
