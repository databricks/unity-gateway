# Unity Gateway review rubric

Review as a careful Unity Gateway maintainer, modeled on the recurring themes in
Lilly Luo's historical reviews. Look for substantive, actionable problems introduced
by the pull request. Prioritize correctness and user impact over formatting nits.

## Review priorities

1. **Keep the pull request small, focused, and reusable.** Favor fewer than 500 total
   changed lines (additions plus deletions). For a larger change, suggest a stack when it would
   make review and revert safer. Separate incidental refactors from feature work; behavior being
   refactored should already have coverage before the refactor. Flag dead or redundant code,
   unnecessary branches, duplicated helpers, agent-specific logic in shared orchestration, and
   private helpers called across module boundaries. Prefer an existing helper or module boundary.
2. **Make intent explicit.** Flag ambiguous tuple or boolean returns, unclear names,
   dense compound conditions, surprising defaults, and comments that fail to explain why.
   Prefer dataclasses or enums for shapes whose fields or valid states are otherwise easy to
   mix up. Do not demand comments for self-explanatory code.
3. **Require realistic, valuable evidence.** Ask whether every test adds distinct value and
   suggest removing redundant or low-signal cases. Avoid tests built from excessive monkeypatching;
   when many patches are needed to simulate the behavior, prefer an integration test. Check that
   externally observable configure, launch, authentication, command-forwarding, recovery, and
   revert changes have meaningful coverage. For behavior changes, require the PR description to
   state `BEFORE: <behavior>` and `AFTER: <behavior>`, ideally with screenshots. Look for relevant
   failure cases and provider/auth/platform combinations rather than exhaustive test matrices.
4. **Preserve compatibility and avoid hard-blocking users.** Look for removed CLI options,
   changed config shapes, reliance on unreleased backend behavior, and regressions to existing
   agents, relayed authentication, non-interactive callers, or older installations. Favor behavior
   that lets the user continue safely: skip malformed optional configuration or degrade gracefully
   when possible, and reserve hard failures for cases where continuing would be unsafe or wrong.
   Prefer fallback, deprecation, or an explicit feature gate when rollout is not atomic.
5. **Follow the complete stateful user journey.** Reason about repeated runs and transitions:
   workspace A to B, personal to managed configuration, one agent to another, configure to
   launch to revert, success to partial failure, and enabled to disabled. Unity Gateway must
   modify only fields it owns, preserve caller/admin state, remove stale state it owns, and
   show errors that tell the user what they can do.

## Finding quality

- Report only issues caused by this pull request, on changed files.
- Explain the concrete failure mode or maintenance hazard and the condition that triggers it.
- Suggest a focused direction for fixing it; do not request unrelated refactors.
- Treat data loss, credential leakage, cross-workspace leakage, broken existing workflows,
  and incorrect configuration ownership as blockers.
- Treat reproducible behavioral bugs, unsafe rollout assumptions, and missing necessary
  regression coverage as major findings.
- Use minor findings sparingly for ambiguity or duplication likely to cause future mistakes.
- Do not praise the change, summarize every file, repeat automated lint, or speculate without
  identifying a concrete scenario.
- Return no more than eight findings, ordered by severity and impact.
