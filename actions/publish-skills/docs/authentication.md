# Authentication and administrator setup

[Back to the quickstart](../README.md)

Complete this once for each publishing identity, repository, and destination.
A Databricks administrator prepares the principal and UC permissions; a GitHub
repository administrator configures the environment. Skill authors can then use
the workflow without handling credentials.

## 1. Prepare Databricks access

1. Confirm UC skills are enabled in your workspace. Create or select a standard
   UC catalog and schema with managed storage, such as `acme.team_skills`.
   Foreign/federated catalogs are not supported. See
   [govern skills](https://docs.databricks.com/aws/en/ai-gateway/govern-skills).
   Workspaces enforcing the Compliance Security Profile block skill creation and
   updates; check feature availability before setting up the workflow.
2. Create a Databricks service principal or reuse an approved CI principal. Assign
   it to the target workspace with workspace access. It does not need workspace
   admin, cluster creation, a SQL warehouse, or a notebook job.
3. Record its **application/client ID** for `DATABRICKS_CLIENT_ID`. This is different
   from its numeric service principal ID, used by the account federation API.
4. As the catalog/schema owner or a principal authorized to grant access, use
   **Catalog Explorer > Permissions > Grant** to give the publishing principal:

   | Object | Privileges | Purpose |
   |---|---|---|
   | Target catalog | `USE CATALOG` | Traverse the catalog |
   | Target schema | `USE SCHEMA`, `CREATE VOLUME` | Traverse the schema and create skills |
   | Existing skills this workflow will update | `READ VOLUME`, `WRITE VOLUME` | Inspect and update their bundles |

   Newly created skills are owned by this principal, which already has access to
   their bundles. Reusing another author's skills requires the explicit grants
   above; changing ownership is not required. Repeat for each mapped destination.
   UC skills use volume privileges even though they are skill securables.

For an existing skill, its owner or a principal with `MANAGE` can grant publishing
access through the API. This is an **administrator command**, not a workflow step.
The Databricks CLI must already be installed and authenticated to the intended
workspace as that grantor. Replace the names, application ID, and profile:

```bash
databricks grants update skill acme.team_skills.sql-guide \
  --profile YOUR_ADMIN_WORKSPACE_PROFILE \
  --json '{"changes":[{"principal":"PUBLISHER_APPLICATION_ID","add":["READ_VOLUME","WRITE_VOLUME"]}]}'
```

## 2a. GitHub OIDC (recommended)

**How it works:** GitHub issues a short-lived identity token for the workflow job.
The Databricks SDK exchanges it for a Databricks OAuth access token using the
service principal's federation policy. Publishing runs as that service principal.
You do not generate or store a Databricks secret in GitHub.

1. In GitHub, open your **skills repository**, then **Settings > Environments >
   New environment**. Create `production`. If your GitHub plan or organization
   restricts environments, have the repository administrator configure a supported
   deployment environment and adjust the workflow and federation policy together.
2. An authorized Databricks account administrator configures a workload identity
   federation policy **on the publishing service principal**, following the
   [Databricks GitHub federation guide](https://docs.databricks.com/aws/en/dev-tools/auth/provider-github).
   Use these values:

   | Policy field | Value |
   |---|---|
   | Issuer | `https://token.actions.githubusercontent.com` |
   | Subject claim | `sub` |
   | Subject | The exact GitHub OIDC subject for **your repository** and `production` environment |
   | Audience | Your Databricks account ID, following the federation guide |

   The subject identifies the customer repository that runs the workflow, **not**
   `databricks/unity-gateway`. With an environment, the subject is an
   environment subject rather than a branch subject. Older repositories typically
   use `repo:YOUR_ORG/YOUR_REPO:environment:production`; newer repositories can
   include numeric organization/repository IDs. Follow GitHub's
   [OIDC subject documentation](https://docs.github.com/en/actions/reference/security/oidc)
   to determine the exact format. Do not copy another repository's subject.
   The SDK normally discovers the Databricks account audience from workspace
   metadata; if your setup requires an explicit override, set
   `DATABRICKS_TOKEN_AUDIENCE` to the same audience as the policy.
3. Under **Settings > Environments > production > Environment variables**, add:

   | Variable | Example / source |
   |---|---|
   | `DATABRICKS_HOST` | Workspace base URL, e.g. `https://company-workspace.cloud.databricks.com`; no `/api` or browser path |
   | `DATABRICKS_CLIENT_ID` | Publishing principal's application/client ID |

   These are variables, not secrets. No client secret is needed for OIDC.
4. Copy [examples/quickstart.yml](../examples/quickstart.yml). Keep
   `permissions: id-token: write`, `environment: production`, and
   `DATABRICKS_AUTH_TYPE: github-oidc`. If you rename the environment, update the
   GitHub environment, job, and federation policy subject together. Restrict the
   environment's deployment branches to your publishing branch when appropriate.
5. Return to [publish and verify](../README.md#4-publish-and-verify).

For administrators using the Databricks CLI, the equivalent policy request is
below. Authenticate an account-level CLI profile first, replace every placeholder,
and use the principal's **numeric ID** for this command. `EXACT_GITHUB_SUBJECT`
must be the repository/environment subject determined above.

```bash
databricks account service-principal-federation-policy create NUMERIC_PRINCIPAL_ID \
  --profile YOUR_ADMIN_ACCOUNT_PROFILE \
  --json '{"oidc_policy":{"issuer":"https://token.actions.githubusercontent.com","audiences":["DATABRICKS_ACCOUNT_ID"],"subject":"EXACT_GITHUB_SUBJECT","subject_claim":"sub"}}'
```

The policy grants trust to the GitHub identity; UC grants separately determine
what it can publish. OIDC is supported by the pinned Databricks SDK. This POC's
live integration testing used OAuth M2M, so it does not establish an end-to-end
OIDC validation result.

## 2b. Databricks OAuth M2M

**How it works:** the SDK exchanges the service principal's application ID and
OAuth secret for short-lived Databricks access tokens and refreshes them as needed.
GitHub stores the OAuth secret; it is not a personal access token.

1. Complete [Databricks access](#1-prepare-databricks-access) for the principal.
2. In Databricks **Settings > Identity and access > Service principals > Manage**,
   select the principal, open **Secrets**, and generate a Databricks OAuth secret
   with an expiry appropriate for your CI rotation policy. Copy the secret when
   shown; it is only displayed once. The publisher requests
   `all-apis`, so this quickstart requires a secret that permits that scope. Limit
   this dedicated principal's resource permissions to the target schema;
   `all-apis` does not grant additional UC privileges. If your organization requires
   narrower OAuth scopes, adapt and validate the publisher's SDK scope configuration
   before using a restricted secret. See the
   [OAuth M2M setup guide](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-m2m).
   For Azure, this example uses a **Databricks-managed** OAuth secret; an Entra
   application secret is a different authentication configuration.
3. In GitHub **Settings > Environments**, create `production`.
4. In that environment, add:

   | Name | GitHub storage | Value |
   |---|---|---|
   | `DATABRICKS_HOST` | Environment variable | Workspace base URL |
   | `DATABRICKS_CLIENT_ID` | Environment variable | Principal's application/client ID |
   | `DATABRICKS_CLIENT_SECRET` | Environment secret | Databricks OAuth secret from step 2 |

5. Copy [examples/quickstart-oauth.yml](../examples/quickstart-oauth.yml). Keep
   `DATABRICKS_AUTH_TYPE: oauth-m2m`. It does not need `id-token: write` or a
   federation policy. Return to [publish and verify](../README.md#4-publish-and-verify).

Rotate the OAuth secret before expiry and replace the GitHub environment secret.
Never paste secrets into workflow YAML, mappings, or commit history.

## 3. Share published skills

Publishing and sharing are separate. The publisher owns new skills and the action
does not grant consumer access. A skill's owner or a principal with `MANAGE` grants
`READ VOLUME` on it to a consumer group through Catalog Explorer. Consumers also
need `USE CATALOG` and `USE SCHEMA` on the parents. Preserve the publishing
principal's read/write access when adjusting grants.

See [govern skills](https://docs.databricks.com/aws/en/ai-gateway/govern-skills#grant-access)
for the full permissions model. Changing the publishing identity later requires
grants on existing skills for the new principal.

## GitHub checkout and Databricks auth are separate

`actions/checkout` reads your GitHub repository using GitHub credentials. The
publishing action uses Databricks SDK unified authentication to write to UC.
Databricks does not clone the repo and needs no linked Git credential. A different
private source repo needs its own GitHub read credential; see
[another source repository](customization.md#publish-from-another-repository).

Your GitHub `production` environment is just a deployment configuration name; it
can point to any chosen workspace. Network access to that workspace is required
for both OAuth and OIDC, independently of whether credentials are valid.
