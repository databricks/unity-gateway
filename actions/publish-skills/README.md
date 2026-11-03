# Publish Git skill folders to Unity Catalog

Publish skills from your GitHub repository to Databricks Unity Catalog when you
merge changes. The action runs on a GitHub runner and calls the Skills and Files
APIs directly. No Databricks notebook, job, or compute is required.

This action is distributed with Unity Gateway under the repository's Databricks
License. Installing the Unity Gateway CLI is not required. See [validation](POC.md)
for tested behavior and limitations.

## Set up in four steps

### 1. Prepare your skill folder and destination

Your repository needs one folder per skill, each containing `SKILL.md`:

```text
skills/
  sql-guide/
    SKILL.md
    references/             # optional supporting files
  code-review/
    SKILL.md
```

For example, `skills/sql-guide/SKILL.md`:

```markdown
---
name: sql-guide
description: Team SQL conventions; use when authoring or reviewing SQL.
---

Use snake_case names and list columns explicitly instead of SELECT *.
```

Choose an existing UC schema, for example `acme.team_skills`. The action maps
`skills/sql-guide` to `acme.team_skills.sql-guide` and includes supporting files.
Discovery checks immediate child folders, not arbitrary nested skill locations.

Ask your Databricks administrator to enable UC skills, prepare managed storage for
the destination schema, and give a publishing service principal workspace access
and the required UC privileges. The action does not create catalogs or schemas.
See [administrator setup](docs/authentication.md#1-prepare-databricks-access).

### 2. Connect GitHub to Databricks once

Choose one authentication method:

| Method | Use it when | Complete instructions |
|---|---|---|
| GitHub OIDC (recommended) | Your administrator can configure workload identity federation; no Databricks secret is stored in GitHub | [Set up OIDC](docs/authentication.md#2a-github-oidc-recommended) |
| Databricks OAuth M2M | You already use a Databricks service principal with an OAuth secret in CI, or want to start with that setup | [Set up OAuth M2M](docs/authentication.md#2b-databricks-oauth-m2m) |

Both methods publish as the service principal, with its UC permissions. Neither
uses your personal Databricks login or GitHub account as the Databricks identity.

In GitHub **Settings > Environments**, create an environment named `production`.
The auth guide explains the exact variables, optional secret, and federation
policy to configure there. The workflow's environment name must match.

For private workspaces or IP restrictions, use a Linux self-hosted runner that can
reach the workspace. Otherwise the examples use GitHub-hosted `ubuntu-latest`.
Your runner also needs access to GitHub and a Python package index. The action
installs Python 3.12 and its dependencies; you do not install the Databricks CLI
on the runner. Check your organization's policy permits this third-party action.

### 3. Copy one workflow and choose two settings

Copy the complete workflow for your authentication method into your repository as
`.github/workflows/publish-skills.yml`:

- **OIDC:** [quickstart.yml](examples/quickstart.yml)
  ([raw file](https://raw.githubusercontent.com/databricks/unity-gateway/main/actions/publish-skills/examples/quickstart.yml)).
- **OAuth M2M:** [quickstart-oauth.yml](examples/quickstart-oauth.yml)
  ([raw file](https://raw.githubusercontent.com/databricks/unity-gateway/main/actions/publish-skills/examples/quickstart-oauth.yml)).

Edit these two settings in the action step:

```yaml
- uses: databricks/unity-gateway/actions/publish-skills@main
  with:
    skills-directory: skills
    target-schema: acme.team_skills
    prune-files: 'true'
```

This snippet is a **step**, not a complete workflow. Copy the linked full file so
checkout, authentication, triggers, serialization, and result reporting are included.
No fork or GitHub App installation is needed.

If you change the source folder, also update the workflow's `paths` filter. If your
default branch is not `main`, update `branches`. For a private workspace, replace
`runs-on: ubuntu-latest` with your connected runner's labels. Use `@main` to follow updates automatically. For reproducible runs, replace `main`
with a reviewed Unity Gateway commit SHA. Existing CLI version tags may predate
this action; choose a ref that contains `actions/publish-skills/action.yml`.

### 4. Publish and verify

The examples publish on matching pushes, including the commit that adds the
workflow. If you want to validate before any automatic publish, initially remove
the `push` trigger and keep `workflow_dispatch`; restore `push` after verification.
The validation checkbox applies only to manual runs.

1. Commit the workflow to your default branch. GitHub only offers manual dispatch
   after the workflow exists on that branch.
2. Open **Actions > Publish skills to Databricks > Run workflow**. Leave
   **Validate folders only** checked for the first run. This checks local content
   without calling Databricks; it does not test authentication or UC permissions.
3. Run it again with that checkbox unchecked. Confirm the job succeeds and the
   summary shows `created`, `updated`, or `unchanged` for every mapped skill.
4. In Databricks **Catalog Explorer**, open the target schema and inspect the skills.
   New skills belong to the publishing service principal. Sharing with teammates
   is a separate UC grant; see [share published skills](docs/authentication.md#3-share-published-skills).
5. Run publishing again without changing the skills. Expect `unchanged` and zero
   uploads/removals. Download `uc-skills-report` from the run's **Artifacts** for
   per-skill details and the checked-out commit SHA.

After setup, pushes to `main` that match the workflow's path filters publish
automatically. These push runs always publish, rather than using the manual
validation checkbox. Tags and releases need their own workflow triggers.

## Map specific folders to specific UC skills

Use this when folders need custom UC names or different destination schemas.
Create `skills.yaml` at the repository root:

```yaml
version: 1
skills:
  - path: skills/sql-guide
    target: acme.team_skills.sql-guide
  - path: .claude/skills/review
    target: acme.team_skills.code-review
```

In your chosen authentication workflow, replace `skills-directory` and
`target-schema` with `mapping-file`:

```yaml
- uses: databricks/unity-gateway/actions/publish-skills@main
  with:
    mapping-file: skills.yaml
    prune-files: 'true'
```

Do not combine those two input modes. There is no inline `skills:` action input.
Include `skills.yaml` and **every mapped source folder** in the workflow's push
`paths`. A complete OIDC mapping workflow is in
[examples/publish-skills.yml](examples/publish-skills.yml); for OAuth M2M, keep the
OAuth environment configuration from your quickstart.

For directory discovery in a mapping file, or a separate source repository, see
[customization](docs/customization.md).

## Update behavior

- Existing skills are updated in place, retaining ownership and grants. Identical
  finalized bundles are skipped after comparing remote file contents.
- `prune-files: 'true'` removes remote files absent from each mapped local folder.
  Use it only for skill bundles this repository should fully manage. When false,
  stale remote files cause that skill to fail before writes.
- Removing a folder or mapping does **not** delete its UC skill. Renaming a target
  creates a new skill and retains the old one. Delete obsolete skills explicitly.
- Updates are **not atomic**. A failure can leave partially updated files; retrying
  converges to the source contents. Other skills can succeed when one fails, but
  the job still fails. Use one publishing repository per target and keep the
  example's concurrency setting.

## Reference

| Input | Default | Purpose |
|---|---|---|
| `skills-directory` | unset | Discover immediate skill folders |
| `target-schema` | unset | Destination `catalog.schema`; required with `skills-directory` |
| `mapping-file` | unset | Custom YAML mappings; defaults to `skills.yaml` if directory inputs are absent |
| `source-directory` | `.` | Source checkout root; source and mapping paths are relative to this root |
| `dry-run` | `false` | Validate content locally without Databricks calls |
| `prune-files` | `false` | Remove stale files inside mapped skills; never deletes a skill |
| `workers` | `4` | Concurrent skill publishers, from 1 to 8 |

`report-file` is an absolute path to JSON results containing source commit,
bundle SHA-256s, statuses, and uploaded/removed counts. The action also writes a
GitHub job summary. Failure reports omit API response bodies; use the exception
type and [troubleshooting guide](docs/troubleshooting.md) to narrow down the cause.

Skill frontmatter needs a valid name and a nonempty description of at most 1024
UTF-8 bytes. UC target names may differ from the frontmatter name. This POC accepts
lowercase UC identifiers starting with a letter or number and containing letters,
numbers, underscores, or hyphens. Paths must stay in the source checkout; symlinks
and nested `.git` content are rejected. Duplicate targets and zero discovered
skills fail validation. All local bundles are held in memory and validated before
writes. Remote comparisons read bundle contents, so large repositories incur
proportional API traffic; 34 skills is the tested POC, not a large-scale benchmark.

- [Authentication and administrator setup](docs/authentication.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Customization, other repositories, and local runs](docs/customization.md)
- [Integration evidence and POC limitations](POC.md)

To change the implementation, fork this repo or copy `action.yml`, `publish.py`,
`requirements.txt`, and the repository-root `LICENSE.md` and `NOTICE.md` into
`.github/actions/uc-skills`. Retain the license and attribution notices when
redistributing your copy. Use
`uses: ./.github/actions/uc-skills` after checkout. Marketplace listing is optional.
Official distribution should use a reviewed Databricks-owned repository and release.

For development, install `requirements.txt`, `pytest`, and `ruff` in a Python 3.12
virtual environment, then run `python -m pytest -q`, `ruff check .`, and
`ruff format --check .` from `actions/publish-skills`.

## Contributing and maintenance

Publisher code and tests live together in this directory. Run the checks in
[test setup](docs/testing.md) before opening a Unity Gateway pull request. The
repository review workflows also cover this action. Code owners are
@sunishsheth2009 and @xsh310. Do not include credentials or private skill content
in public issues or test reports. Report vulnerabilities through
[Databricks Security Response](https://www.databricks.com/trust/security-response).

## License

This action uses the repository's [Databricks License](../../LICENSE.md).
See [NOTICE.md](../../NOTICE.md) for attribution. Keep both files if redistributing
an action copy.
