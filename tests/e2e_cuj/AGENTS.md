# Dedicated-workspace CUJs

Each CUJ owns a separate workspace. Subclass `BaseCujTest` from `base.py` and set
`WORKSPACE_URL`. Setup provides `self.workspace`, a Databricks SDK client using
`UG_CUJ_SP_CLIENT_ID` and `UG_CUJ_SP_CLIENT_SECRET` with OAuth M2M authentication.
Do not share the workspace between concurrent runs. No stubbed configuration.

Run from the repository root:
`uv run pytest --confcutdir=tests/e2e_cuj tests/e2e_cuj`.
This keeps the parent suite's mocked fixtures out of CUJs.

`test_cuj_smart_routing.py` runs six fresh interactive sessions: routed, explicit
model, and routing disabled, for both Claude and Codex. It uses the published
models/defaults and restores the original config on teardown. Model evidence is
from native client records, not independent server-side telemetry.

Use a clean disposable POSIX runner without existing machine-wide agent settings.
Install the intended versions of `ug`, `claude`, `codex`, and `databricks` on PATH.
The terminal helpers also require `pexpect==4.9.0` and `pyte==0.8.2`:
`uv run --with pexpect==4.9.0 --with pyte==0.8.2 pytest --confcutdir=tests/e2e_cuj tests/e2e_cuj -v`.
Collection with `--collect-only` does not authenticate or contact a workspace.
Only the short-lived bearer is forwarded to agent processes, never the SP secret.

The smart-routing fixture claims `/Shared/ug-e2e-workspace-reservation` before any
config changes or inference. Contention fails immediately. Unverified cleanup
retains the reservation; inspect its owner, restore the policy, and stop the old
runner before an administrator removes it. Do not edit a reserved workspace.
The claim coordinates these tests, not arbitrary admin writes. Artifacts are
written beneath pytest's per-test temporary directory, printed during setup.
