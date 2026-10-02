"""Future smart-routing CUJ specification; no executable tests yet.

Planned tests: test_cuj_smart_routing_claude and test_cuj_smart_routing_codex.

Scenario: each test gets its own exclusively assigned Databricks workspace,
including across concurrent runs. Each publishes the same both-agent fixture
there; the Claude and Codex tests never share a workspace. Both agents use that
workspace's published CodingAgentConfig with
smart routing enabled over supported system.ai models. No MPS or active budget
tier participates, and tracing is disabled. Configure using the public ug CLI,
launch interactively without an override, and submit a unique file task through
the real first-prompt path. Repeat in a fresh session with an explicit supported
non-default model. Republish with BOTH smart_routing.enabled flags false,
reconfigure the existing isolated home, and launch a fresh default session.

Published fixture: Claude offers system.ai.claude-opus-4-8,
system.ai.claude-sonnet-4-6 (default), and system.ai.claude-haiku-4-5.
Codex offers system.ai.gpt-5-6-sol (default) and system.ai.gpt-5-6-luna.
The workspace default agent is CODING_AGENT_CLAUDE_CODE, with spec_version 1.

Expected: correlate a successful live routing decision to the submitted prompt
and session, match the selected model to the completed native inference turn,
and require the final assistant answer to contain an unpredictable file value
absent from the prompt. The selected model must be a supported system.ai target
under the live router contract; do not require a particular winner or different
choices for different prompts. A banner alone never proves applied routing.
Explicit models must be honored without new routing decisions. After disabling
routing, tasks must complete on ordinary configured defaults without new
decisions. Snapshot evidence per session to exclude stale routing records.

Claude evidence uses assistant response model metadata. Codex evidence links
the completed rollout turn to its model context; this establishes client
execution, not independent downstream provider identity.

Lifecycle requirements: allocate the workspace per test, isolate machine-wide
settings on a disposable runner, and verify cleanup even on failure. Keep the
workspace exclusively assigned through all phases and cleanup; quarantine it if
cleanup fails. Never retry a failed task, fall back to a shared workspace, or
overwrite unexpected admin edits. Leave actionable recovery evidence on failure.
These mechanisms will be implemented with the live tests, not this scaffold.
"""
