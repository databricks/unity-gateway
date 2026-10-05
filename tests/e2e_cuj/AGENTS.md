# Dedicated-workspace CUJs

Each CUJ owns a separate workspace. Subclass `BaseCujTest` from `base.py` and set
`WORKSPACE_URL`. Setup provides `self.workspace`, a Databricks SDK client using
`UG_CUJ_SP_CLIENT_ID` and `UG_CUJ_SP_CLIENT_SECRET` with OAuth M2M authentication.
CUJs that mutate workspace or budget configuration must not share their target
between concurrent runs. Read-only CUJs may share a workspace on separate runners.
No stubbed configuration.

Run from the repository root through the isolated integration runner:

```bash
uv run --no-project --python 3.12 python scripts/run_integration.py \
  --suite e2e-cuj --ug-version checkout \
  --claude-version 2.1.280 --codex-version 0.154.0 -- -m cuj
```

The runner installs the pinned Databricks SDK and both agent CLIs, then invokes
pytest with this directory as its `--confcutdir`. The default marker for this
suite is `cuj`; keep the explicit `-m cuj` when reproducing CI.
