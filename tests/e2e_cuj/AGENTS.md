# Dedicated-workspace CUJs

Each CUJ owns a separate workspace. Subclass `BaseCujTest` from `base.py` and set
`WORKSPACE_URL`. Setup provides `self.workspace`, a Databricks SDK client using
`UG_CUJ_SP_CLIENT_ID` and `UG_CUJ_SP_CLIENT_SECRET` with OAuth M2M authentication.
Do not share the workspace between concurrent runs. No stubbed configuration.

Run from the repository root:
`uv run pytest --confcutdir=tests/e2e_cuj tests/e2e_cuj`.
This keeps the parent suite's mocked fixtures out of CUJs. There are no tests yet.
