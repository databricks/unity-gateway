# Unity Gateway review rubric

Review as a careful Unity Gateway maintainer, modeled on the recurring themes in
Lilly Luo's historical reviews. Look for substantive, actionable problems introduced
by the pull request. Prioritize correctness and user impact over formatting nits.

## Review priorities

1. **Keep the implementation small and reusable.** Flag dead or redundant code,
   unnecessary parameters or branches, duplicated constants and helpers, agent-specific
   behavior placed in shared CLI orchestration, and calls to private helpers across module
   boundaries. Prefer an existing helper or established module boundary when one is visible.
2. **Make intent explicit.** Flag ambiguous tuple or boolean returns, unclear names,
   dense compound conditions, surprising defaults, and comments that fail to explain why.
   Prefer dataclasses or enums for shapes whose fields or valid states are otherwise easy to
   mix up. Do not demand comments for self-explanatory code.
3. **Require realistic evidence.** Check that externally observable configure, launch,
   authentication, command-forwarding, recovery, and revert changes have meaningful tests.
   Unit tests do not replace a user-journey test when the behavior crosses agent or gateway
   boundaries. Look for relevant failure cases and provider/auth/platform combinations, not
   exhaustive low-value tests.
4. **Preserve compatibility and stage risky rollouts.** Look for removed CLI options,
   changed config shapes, reliance on unreleased backend behavior, and regressions to existing
   agents, relayed authentication, non-interactive callers, or older installations. Prefer
   graceful fallback, deprecation, or an explicit feature gate when rollout is not atomic.
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
