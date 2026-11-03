# Troubleshooting

[Back to the quickstart](../README.md)

Start with the failed step's log and download the `uc-skills-report` artifact if
present. The report includes per-skill statuses and error types, but intentionally
omits upstream API error bodies. A dependency/setup failure can happen before a
report exists. Never print tokens or client secrets while diagnosing auth.

| Symptom | What to check |
|---|---|
| Workflow is missing from the Actions tab or has no Run workflow button | Commit it under `.github/workflows/` on the default branch; include `workflow_dispatch`; enable GitHub Actions in the repo. |
| Third-party action is blocked or cannot be resolved | Check the organization's allowed-actions policy and the repository/release ref. Use an approved fork or local copy if required. |
| Job waits for environment approval | A protection rule on `production` requires approval. This is configured by your GitHub administrator. |
| Workspace URL or client ID is empty | Add variables to the exact GitHub environment named by the job. Use environment **variables**, not secrets, for the two values read through `vars`. |
| OIDC cannot obtain a GitHub token | Keep `permissions: id-token: write`; this auth type works inside GitHub Actions. Use a Databricks profile for local runs. |
| OIDC exchange fails | Match issuer, audience, and the repository/environment subject exactly; confirm application ID and workspace assignment. A branch subject will not match an environment job. Newer repos may use numeric IDs in `sub`. |
| OAuth M2M fails | Use a Databricks OAuth secret for the configured principal, verify it has not expired, and check the environment secret name. Do not substitute a PAT or an Entra secret in this example. |
| `PermissionDenied`, `Forbidden`, or access denied | Verify the publishing principal's workspace assignment, API scopes, and UC grants on the catalog, schema, and existing skills. Auth success alone does not grant UC access. |
| `NotFound` / schema missing | Create the target schema first and confirm the workspace is attached to its metastore. The action never creates schemas. |
| Storage or feature error on creation | Confirm UC skills are enabled, the catalog is standard, and managed storage resolves for the schema. Check Compliance Security Profile restrictions. |
| Timeout, network denial, or staging ingress rejection | Use a Linux runner with access to the workspace and its auth endpoints. Public GitHub runners may be blocked by VPN, private networking, or IP restrictions. |
| Python/package installation or CA error | The runner needs access to GitHub and a package index. Configure your organization's trusted CA bundle and package mirror on self-hosted runners; keep certificate verification enabled. |
| Zero skills or missing SKILL.md | Directory mode discovers immediate child folders with `SKILL.md`. Check spelling, case, source-directory, and checkout contents. Use explicit mappings for other layouts. |
| Invalid YAML, description, name, symlink, or duplicate target | Correct the reported local bundle/mapping error and retry with manual validation enabled. |
| Stale remote files | Set `prune-files: 'true'` only if this repository should fully own that bundle's file set. Otherwise reconcile the bundle manually. |
| Merge did not publish | Check branch and `paths` filters, including mappings and every source folder. Manually run the workflow to publish immediately. |
| Some skills failed but others exist | Publishing is per skill. Fix the failure and rerun; completed bundles are compared and skipped if unchanged. Updates are not atomic. |
| A skill removed from Git is still in UC | Whole-skill deletion is intentionally manual. Removing a source folder or mapping does not delete its UC skill. |
| Consumer cannot find/read a published skill | Grant consumer `READ VOLUME` plus parent `USE CATALOG` and `USE SCHEMA`; check publisher sharing setup. |

A successful **Validate folders only** run proves local bundle validation, not
Databricks authentication, connectivity, permissions, or successful publication.
After a live run, inspect the target schema and rerun unchanged to verify convergence.
