# Customize publishing

[Back to the quickstart](../README.md)

Keep the checkout, authentication settings, and concurrency group from your chosen
quickstart. Change mappings or triggers to match your release process.

## Discover folders from a mapping file

Instead of listing every folder, `skills.yaml` can discover each immediate child
containing `SKILL.md`:

```yaml
version: 1
directories:
  - path: skills
    schema: acme.team_skills
```

Use `mapping-file: skills.yaml` in the action step. Explicit `skills` entries and
`directories` entries can coexist when their targets do not overlap. Mapped UC
names can differ from frontmatter names. Target names are not silently normalized.

## Publish from another repository

Check out the source repository into its own directory, then point the action at
that checkout. For a public source repo, add this checkout and publishing step to
your authenticated workflow:

```yaml
- uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
  with:
    repository: YOUR_ORG/YOUR_SKILLS_REPO
    ref: main
    path: source-skills
    persist-credentials: false
- uses: databricks/unity-gateway/actions/publish-skills@main
  with:
    source-directory: source-skills
    skills-directory: skills
    target-schema: acme.team_skills
    prune-files: 'true'
```

For custom mappings, put `skills.yaml` inside `source-skills` or copy it there
before publishing; paths are relative to `source-directory`. A separate private
source repo needs a scoped GitHub App token or another approved read credential
passed to checkout's `token` input. The caller repo's default token generally does
not grant cross-repository private access.

A push to the source repo does not automatically trigger a workflow in a different
repo. Install the workflow in the source repo, or configure a schedule/manual
trigger or an approved cross-repo dispatch. The reported commit is the checked-out
source commit. GitHub OIDC trust still identifies the repository running the job.

## Validate pull requests

Copy [examples/validate-skills.yml](../examples/validate-skills.yml) to
`.github/workflows/validate-skills.yml`. This workflow has no Databricks credentials
and uses `dry-run: 'true'`. It validates content without testing remote state,
authentication, grants, or connectivity. Publish through your trusted branch.

## Branches, tags, and releases

The quickstarts publish on matching pushes to `main` and support manual runs.
For version-based publishing, configure a tag or release trigger and choose the
checkout ref explicitly. If using an environment federation subject, keep the
environment aligned with its policy and allowed deployment branches/tags. Use a
separate schema/environment for development if that fits your team.

Keep `cancel-in-progress: false` so a newer commit does not interrupt uploads.
Use the same concurrency group for workflows in one repo that write the same
targets. GitHub concurrency groups do not coordinate separate repositories; use
one publishing repository per target. The action does not provide atomic bundle
replacement, rollback, version retention, or automated whole-skill deletion.

## Run the publisher locally

Clone this action repository and create a Python 3.12 virtual environment there:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python publish.py --source-directory /path/to/your/skills-repo \
  --skills-directory skills --target-schema acme.team_skills --dry-run
.venv/bin/python publish.py --source-directory /path/to/your/skills-repo \
  --skills-directory skills --target-schema acme.team_skills \
  --profile YOUR_WORKSPACE_PROFILE --prune-files --report reports/publish.json
```

For live local runs, first install/authenticate the Databricks CLI to the intended
workspace and select that profile with `--profile`. For custom mappings, replace
`--skills-directory` and `--target-schema` with `--mapping-file skills.yaml`.
Do not use `github-oidc` outside a GitHub Actions job.
