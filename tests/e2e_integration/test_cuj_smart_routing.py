"""Specification for future smart-routing CUJs; no executable tests yet.

Planned tests: test_cuj_smart_routing_claude and test_cuj_smart_routing_codex.

Scenario: both agents use the workspace's published CodingAgentConfig with
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

Lifecycle requirements: isolate machine-wide settings on a disposable runner,
coordinate all participating publishers through one cross-machine workspace
lock, and restore and verify the original policy even on failure. Never retry a
failed task, steal a live lock, or overwrite unexpected concurrent admin edits.
Leave actionable recovery evidence if restoration fails. These mechanisms will
be implemented with the live tests, not as part of this scaffold.
"""
