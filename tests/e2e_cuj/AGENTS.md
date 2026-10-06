# Dedicated-workspace CUJs

Each CUJ owns a separate workspace. Subclass `BaseCujTest` from `base.py` and set
`WORKSPACE_URL`. Setup provides `self.workspace`, a Databricks SDK client using
`UG_CUJ_SP_CLIENT_ID` and `UG_CUJ_SP_CLIENT_SECRET` with OAuth M2M authentication.
All workspace configurations are READ ONLY. Never create, publish, update, delete,
or restore workspace configuration, including during setup or teardown. Validate
the preconfigured workspace and fail on a mismatch; never repair it. Concurrent
runs may read the same CUJ workspace. Do not add reservations or remote lock files.
Keep local homes, task files, and artifacts isolated per run. No stubbed configuration.
Use `CLAUDE` / `CODEX` from `helpers/constants.py`; reject unsupported agents explicitly.

The budget-default CUJ uses two marker-selected runs against the fixed tier with
`spending_percentage=0.0001` (0.01%). The shared-principal run uses
`UG_CUJ_SP_CLIENT_ID` and `UG_CUJ_SP_CLIENT_SECRET` with `-m 'not cuj5_below_tier'`;
the below-tier run uses `UG_BUDGET_CUJ_SP_CLIENT_ID` and
`UG_BUDGET_CUJ_SP_CLIENT_SECRET` with `-m cuj5_below_tier`. CI maps the selected
identity into the standard
`UG_CUJ_SP_CLIENT_ID` / `UG_CUJ_SP_CLIENT_SECRET` fixture variables. Both cases
read the workspace and budget recommendation; below-tier bare `ug` selects
Claude/Sonnet, while above-tier read-only `ug usage` agrees with backend spend,
threshold, and percentage and bare `ug` selects Codex/Luna over Sol. Neither case
writes workspace or budget configuration or submits an inference task.

Before adding any helper, search the existing test utilities and installed SDK for
equivalent functionality. Reuse them directly; never build a parallel HTTP/auth
client, session harness, terminal driver, or workspace CRUD layer. Workspace
helpers must use the base class's authenticated `self.workspace` client: generated
SDK read methods first, `api_client.do` with GET for uncovered config/catalog
endpoints. Keep only CUJ-specific behavior and explain why existing helpers cannot
provide it. Do not add a framework or abstraction for hypothetical future tests.

Run from the repository root:
`uv run pytest --confcutdir=tests/e2e_cuj tests/e2e_cuj`.
This keeps the parent suite's mocked fixtures out of CUJs.

Use `helpers/tui_request_recorder.py` when a CUJ must assert the real HTTP requests
made by an interactive agent. Start one recorder per class-scoped CUJ scenario,
configure its isolated `ug` session with the recorder URL, and keep the CUJ's
workspace URL as the recorder's upstream. Use sequence checkpoints to separate
fresh TUI sessions.
Assert individual JSON fields through `RecordedRequest.payload`, for example
`request.payload["task"]["prompt"] == expected_prompt`. Raw bytes remain available
as `RecordedRequest.body`. Authorization, cookie, and token headers are redacted.
Use `recorder.response_for(request)` to inspect the status, headers, or JSON
payload returned in response to that exact request.

Reuse `tests/integration/utils` session, terminal, file-task, and transcript helpers
directly. Importing helpers does not load that suite's conftest. Never use its
managed-config stub helpers. CUJ adapters retain strict routing/model correlation,
reject unexpected permission prompts, and keep local agent settings in temporary homes.
In `helpers/evidence.py`, `ClaudeCujHelper` and `CodexCujHelper` implement
`_completed_turn` and `_assert_applied`; `BaseCujHelper` holds common model checks.
`get_cuj_helper` is the single agent selector and rejects unsupported agents.

Model evidence combines observed gateway requests/responses with native completed-turn
records; neither routing banners nor native records alone prove an applied decision.

Use a clean disposable POSIX runner without existing machine-wide agent settings.
Install the intended versions of `ug`, `claude`, `codex`, and `databricks` on PATH.
The terminal helpers also require `pexpect==4.9.0` and `pyte==0.8.2`:
`uv run --with pexpect==4.9.0 --with pyte==0.8.2 pytest --confcutdir=tests/e2e_cuj tests/e2e_cuj -v`.
Append `-m 'not cuj5_below_tier'` for the shared-principal run or
`-m cuj5_below_tier` for the dedicated low-spend run, after setting the standard
credential variables to the selected identity. Collection with `--collect-only`
does not authenticate or contact a workspace. Only the short-lived bearer is
forwarded to agent processes, never the SP secret.

Artifacts are written beneath pytest's per-scenario temporary directory, printed during setup.
