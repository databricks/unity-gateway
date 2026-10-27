---
service: unity-gateway
created: 2026-10-12
---

# Install and Refresh Machine-Managed Workspace Policy

## Intent

Install workspace policy in `/etc/unitygateway/managed-settings.json` during `ug configure` and read it during agent launches. Launches replay the saved read instead of refreshing it (superseded: the five-minute TTL was removed, so an admin's change is picked up by the next `ug configure`); never rewrite the privileged file or requesting sudo when policy content is unchanged.

## Context

Previously, `managed_config.py` stored the remote `CodingAgentConfig` in `~/.ucode/managed-config.json` alongside `workspace`, `retrieved_at`, and `outcome`. Configure forced a refresh; launches and indirect agent checks reused that wrapper for five minutes.

The existing privileged writer atomically installs allowlisted Claude/Codex settings. Extend it for Unity Gateway policy without changing agent settings metadata behavior. Agent-specific OS-managed files remain separate from the workspace policy.

## Requirements

- R1: Explicit workspace configuration fetches policy through the existing authenticated `GET /api/ai-gateway/v2/coding-agent-configs`.
- R2: Install only `{workspace, config}`, preserving the raw remote configuration under `config`; no refresh timestamps or outcomes in the protected wrapper.
- R3: Bare `ug`, `ug <agent>`, and indirect agent checks reuse fresh refresh metadata and check remote policy after the five-minute TTL.
- R4: The installed machine file is the sole persisted policy source; the legacy home cache is neither read nor automatically migrated.
- R5: Canonical workspace URLs determine identity. The installed workspace is authoritative for launches and refresh; reject a different selected workspace instead of falling back to unmanaged behavior.
- R6: Install atomically with root-owned `0755` directory and root-owned `0644` file; reject unsafe targets and add no authentication data.
- R7: Failed privileged configure writes raise actionable errors. Failed launch writes warn and continue with the matching installed policy.
- R8: No-config or feature-disabled removes only a matching installed policy. If none exists, do not create a file or directory to represent absence.
- R9: Retain existing forgiving absent/malformed-policy reads and fetch-failure fallback. Unsupported remote spec versions retain existing diagnostics.
- R10: Preserve resolution and agent-specific rendering; new policy refresh fallback does not bypass independent agent OS-managed-file conflicts.
- R11: Dry-run configuration previews required policy changes without sudo or disk changes; launch dry-run is local and offline.
- R12: Export and local status use installed policy; explicitly requested live status may query remote policy without installing it.
- R13: Store workspace-keyed check timestamps, outcomes, and installed-file fingerprints in `~/.ucode/refresh_ts.json`, mode `0600`, without a second policy copy.
- R14: Compare workspace and raw config excluding only volatile API `retrieved_time`; unchanged reads never rewrite protected policy.
- R15: Only explicit configure may invoke sudo (interactive prompt or scripted `sudo -n`); root provisioning needs no sudo. Non-root launch policy writes/removals are deferred without even a sudo capability probe.
- R16: Deferred launch updates direct users to IT/an administrator, not to a configure command they cannot run.
- R17: Document that the arbitrary-string header schema does not prohibit secrets and that the entire readable raw policy must contain no secret values.
- R18: Warn once per distinct pending policy change across TTL expiry, storing only its hash in `refresh_ts.json`; clear the hash on reconciliation.
- R19: Validate launch workspace before cache reuse, authentication, or bootstrap, including dry-run and skip-preflight. Use the installed workspace when none is selected; only explicit administrator configure may replace it.

## Clarifications

- Q1: Which platforms are supported?
  A1: Linux and macOS use `/etc/unitygateway/managed-settings.json`. Unsupported-platform writes fail explicitly; do not introduce a user-cache policy fallback.
- Q2: How many workspaces does the machine file hold?
  A2: One active workspace. Explicit administrator configure can replace it for all users. Launch refuses a mismatched workspace and never uses unmanaged fallback to escape the installed policy.
- Q3: What if sudo is unavailable?
  A3: Configure installation fails. Launch installation warns and retains the old matching policy; if none is installed, the fetched policy applies to that run without installing it.
- Q4: Why not use the installed file's inode timestamps for refresh throttling?
  A4: Those timestamps describe local writes, not a successful unchanged remote check. Keep one user-owned check timestamp plus a fingerprint to invalidate stale checks after another process changes installed policy.
- Q5: What permissions apply?
  A5: Root-owned policy `0644`, directory `0755`; ordinary users need no sudo to read. Refresh metadata is user-owned `0600`.
- Q6: Why retain refresh outcomes?
  A6: Distinguish published, absent, and feature-disabled reads during the TTL without empty policy tombstones or duplicate content. Failed launch updates cache the effective installed fallback, delaying reattempt until TTL expiry.
- Q7: Is the local config identical to the latest remote response?
  A7: `config` preserves raw fields when installed. Only API `retrieved_time` is ignored for drift comparison, so that informational field may remain from an earlier read until actual policy changes.
- Q8: Does Bill's review request policy-controlled refresh intervals?
  A8: The user confirmed that the installed workspace should select the refresh source, with mismatched launch workspaces refused. Keep the five-minute TTL; the supported schema has no interval field.
- Q9: Where is the deployment's 24-hour MDM refresh default?
  A9: UNRESOLVED. No scheduler exists in this PR's baseline; freshly fetched main has a one-shot `scripts/mdm-bootstrap.sh` with no refresh interval. Recommend four hours (`14400` seconds) in deployment scheduling, but the actual scheduler location is needed to change its default.

## Acceptance Criteria

- AC1: GIVEN published policy WHEN configure succeeds THEN the API is queried and the installed wrapper contains its canonical workspace and raw configuration.
- AC2: GIVEN a expired check and unchanged policy WHEN an agent launches THEN only user refresh metadata advances; installed bytes, inode, mtime, and ctime stay unchanged and no sudo occurs.
- AC3: GIVEN a fresh matching check WHEN bare `ug` launches THEN it reads installed policy without another policy API request.
- AC4: GIVEN configure already refreshed policy WHEN Claude or AI Tools checks it again THEN the shared refresh metadata prevents a duplicate API request or privileged write.
- AC5: GIVEN only legacy home policy WHEN a launch refresh cannot install policy THEN the legacy copy is not read or promoted.
- AC6: GIVEN installed machine policy WHEN launch selects a foreign workspace THEN launch fails before cache/auth/bootstrap, without requests or policy/state writes; dry-run and skip-preflight do not bypass the guard.
- AC7: GIVEN matching installed policy WHEN no-config or feature-disabled is fetched and removal succeeds THEN the file is removed and the outcome is cached only in refresh metadata. GIVEN another workspace's installed policy WHEN `ug configure` selects a workspace without policy THEN that policy is removed; a launch never removes it.
- AC8: GIVEN installed policy WHEN privileged replacement fails THEN bytes remain intact; configure errors and launch warns while returning the installed policy.
- AC9: GIVEN failed API/authentication reads WHEN launching THEN existing matching-policy fallback and unsupported-version diagnostics remain.
- AC10: GIVEN root-owned readable policy and a fresh check WHEN an ordinary user launches THEN no sudo is needed to read it.
- AC11: GIVEN dry-run configuration WHEN policy needs installing THEN preview occurs without writes or sudo; GIVEN launch dry-run WHEN invoked THEN no policy API request occurs.
- AC12: GIVEN successful installation WHEN metadata is inspected THEN directory is root-owned `0755`, file root-owned `0644`, and refresh metadata `0600`.
- AC13: GIVEN symlinked or unsafe targets WHEN installing THEN replacement is refused.
- AC14: GIVEN valid installed policy WHEN export or local status runs THEN the installed file is the policy source.
- AC15: GIVEN changed policy during launch setup WHEN a non-root user launches THEN no sudo probe, password prompt, or protected write/removal occurs; IT guidance and installed fallback remain. GIVEN root refresh THEN installation proceeds directly without sudo.
- AC16: GIVEN remote changes only to `retrieved_time` WHEN TTL expires THEN no protected write or sudo occurs.
- AC17: GIVEN another process replaces or edits installed policy WHEN a still-warm refresh runs THEN its fingerprint invalidates the check.
- AC18: GIVEN workspace without policy and no matching installed file WHEN refreshed THEN no protected file or directory is created.
- AC19: GIVEN deferred launch update WHEN immediately relaunched THEN the recorded check throttles another API request/warning within five minutes.
- AC20: GIVEN explicit configure WHEN changed policy requires privilege THEN existing interactive/scripted sudo behavior remains available.
- AC21: GIVEN a deferred policy change WHEN repeated launch checks expire THEN only the first check warns and all checks retain installed policy; a distinct fetched policy warns again.
- AC22: GIVEN a pending hash WHEN retrieval metadata or JSON object key order alone changes THEN no new change warning occurs.
- AC23: GIVEN a pending update/removal WHEN remote policy matches installed policy or root/MDM/configure reconciles it THEN the next successful check clears the hash.
- AC24: GIVEN installed policy and no selected workspace WHEN launch checks policy THEN the installed workspace is the remote source, while per-user authentication remains per-user.

## Constraints

- Fetching, normalization, policy persistence, and throttling remain in `managed_config.py`; orchestration stays in `cli.py`; privileged writing stays in `managed_files.py` and its shell worker.
- Extend fixed target allowlists narrowly. Preserve existing agent metadata and immutable-flag handling.
- Keep tokens/profiles in existing user state, never add them to the policy wrapper.
- Use existing Rich warning/error conventions and atomic JSON persistence.
- Unit tests isolate paths and privileged operations. Live integration requires a disposable clean machine with explicit credentials; never mutate this developer's real OS-managed files.

## Out of Scope

- Remote API changes, policy authoring, or publishing.
- Multiple simultaneous machine-workspace policies.
- Background refresh or complete tamper-proof enforcement.
- Replacing Claude/Codex OS-managed files or their independent conflict checks.
- Automatic legacy home-policy promotion.
- Mandatory launch failure on denied policy update.

## Plan

### Approach

1. Change the authoritative policy path and persist only workspace/config. Normalize at consumption time; ignore only volatile retrieval metadata for comparison.
2. Extend the existing atomic privileged writer with fixed policy installation/removal, root ownership, readable modes, configure-only sudo, a no-elevation launch guard, and root support.
3. Move check metadata into the user-owned sidecar. Keep five-minute throttling, negative outcomes, fingerprint invalidation, and a pending-policy hash for once-per-change warnings without copying policy.
4. Force refresh in all explicit agent configuration variants. Keep launch and indirect agent refreshes; after one check they reuse the sidecar.
5. Handle changed installs and matching removals through the writer. Configure errors on failed writes; launches warn once per distinct change and use the installed fallback, throttling retries. Clear pending hashes after reconciliation.
6. Update export/recovery documentation and integration consumers. Validate temporary-path unit/component coverage and integration contracts; report live sudo coverage as unrun without a clean VM.
7. Use the installed workspace as launch/refresh authority. Refuse mismatched state/flags before cache/auth/bootstrap; allow explicit administrator configure to switch the machine workspace.

### Affected Components

- `src/ucode/managed_config.py`: machine path, raw wrapper comparison, user timestamp cache, effective-policy fallback.
- `src/ucode/managed_files.py`, `managed_replace.sh`: narrowly allowlisted atomic installation/removal and ownership/modes.
- `src/ucode/cli.py`: consistent explicit-agent configure refresh sequencing.
- `managed_setup.py`, `managed_resolve.py`, `managed_export.py`: source-path documentation and recovery guidance.
- Unit/component tests and integration journeys/helpers/runner: separate policy/refresh paths, unchanged-file evidence, clean-machine prerequisite.

### Data Model Changes

Machine policy is `{workspace, config}`. User refresh metadata maps each canonical workspace to `{last_checked_at, outcome, policy_fingerprint}` plus optional `pending_config_hash` for deferred updates/removals. The SHA-256 hash covers workspace and raw policy excluding `retrieved_time`, with sorted JSON keys. Fingerprint contains existence, device, inode, size, mtime, and ctime. No second raw/normalized policy copy is introduced.

### API Changes

None. Continue using the existing coding-agent-configs GET. Internal policy save supports an interactive flag; privileged update supports install/remove and no-password script behavior.

### Risks and Trade-offs

- Root-writable versus root-readable: policy can be read without sudo, but all machine users can see its configuration metadata. Arbitrary-string headers are not schema-safe for secrets; all raw policy fields must contain no secrets.
- A single active machine workspace is intentional; launch selections must match it and fail rather than run unmanaged. Administrator configure switches affect other users.
- A user-owned timestamp avoids pointless privileged writes after unchanged checks but is mutable and not a security enforcement boundary.
- TTL avoids every-launch API latency but delays change detection by up to five minutes. Fingerprints detect local changes, not remote freshness.
- Warning/fallback favors availability over strict latest-policy enforcement. Denied updates/removals leave old policy active; retries are throttled for five minutes and warnings deduplicated across expiry by pending hash.
- Non-root launch writes are always deferred, even for administrators with cached sudo; this deliberately favors MDM ownership and zero elevation attempts over automatic privileged launch repair. Configure retains interactive sudo and scripted `sudo -n`; root refresh writes directly.
- Ignoring `retrieved_time` avoids false drift but means that field does not necessarily represent the latest check; use the sidecar instead.
- Deletion avoids empty tombstones; sidecar outcomes preserve absent/disabled semantics.
- Refresh metadata atomic writes do not serialize simultaneous workspace checks; a lost timestamp/hash entry can cause an extra check/warning, not an alternate policy copy.
- Legacy home policy is intentionally not trusted/promoted; configure must retrieve/install authoritative workspace policy.
- Existing forgiving readers remain, and this is not a fail-closed enforcement redesign.
- Packaged-writer root ownership and non-root TTY/script deferral are checked in a disposable Linux container. Actual configure password interaction and full MDM/agent provisioning still require separately selected credentials and machine policy.

## Tasks

### Phase 1 — Policy Storage

- [x] T1: Resolve platforms, workspace scope, privilege behavior, TTL, and launch fallback.
- [x] T2: Introduce machine policy and workspace-aware raw/normalized readers.
- [x] T3: Compare raw policy excluding volatile retrieval metadata.
- [x] T4: Update persistence and legacy-cache tests.

### Phase 2 — Privileged Installation

- [x] T5: Extend both fixed privileged allowlists.
- [x] T6: Add atomic installation, enforced policy modes, and dry-run behavior.
- [x] T7: Refresh all explicit agent configuration variants.
- [x] T8: Remove matching policy for absent/disabled responses and preserve failed writes.

### Phase 3 — Throttled Launch Refresh

- [x] T9: Add workspace-keyed user-owned refresh metadata.
- [x] T10: Preserve five-minute launch throttling and local-only launch dry-run.
- [x] T11: Share throttling with indirect Claude/AI Tools refresh checks.
- [x] T12: Cover unchanged policy, retrieval-only drift, configure-only sudo, deferred launch fallback, no-probe guards, and fingerprint invalidation.

### Phase 4 — Consumers and Documentation

- [x] T13: Update installed-policy source and export recovery guidance.
- [x] T14: Update integration consumers and clean-machine runner checks.
- [x] T15: Document permissions, refresh lifecycle, single-workspace scope, and migration.

### Phase 5 — Review Follow-ups

- [x] T16: Hash pending workspace policy, suppress repeat warnings, and clear the hash on reconciliation.
- [x] T17: Guard all launch paths against workspace mismatch before setup and use installed workspace for remote refresh.
- [x] T18: Add unit regressions and a black-box foreign-workspace rejection journey; document the four-hour MDM scheduling recommendation.
- [ ] T19: Change the actual MDM refresh default to four hours once its scheduler location is supplied; no such default exists in this PR or fetched main.

### Validation

- [x] T-val: Rerun focused tests, integration contracts, lint, formatting, types, and root/non-root container component checks after review fixes. Focused checks: 1009 passed; real container root ownership/modes, root/non-root persistence, and installed-CLI workspace rejection checks passed. Managed integration file: 5 cases collected, not a live pass. Prior full suite: 2783 passed, 44 skipped, 9 unrelated/environment failures (documented in the PR). Full live MDM/agent provisioning remains pending explicitly selected workspace credentials; the MDM scheduler source is still needed for T19.
