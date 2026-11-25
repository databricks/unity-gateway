---
name: smart-router-orchestrator
description: Delegate substantive work through Copilot's native task tool while Smart Router selects each child's model.
---

# Smart Router Orchestrator

UG activated this workflow for this smart-routed Copilot session. Follow user
overrides and preserve the parent's model and reasoning effort. The parent owns
acceptance, cross-owner decisions, and the final response.

Choose ownership before gathering source evidence. For investigation,
implementation, debugging, or verification beyond retained facts and a bounded
lookup, delegate the complete outcome to one native subagent first. Knowing
filenames or entrypoints is not having the evidence needed to answer. A connected
investigation is one assignment even when it has no independent parallel branch.
Answer directly for small requests or when delegation is prohibited or unavailable.

Use Copilot's native `task` tool with `agent_type: general-purpose`. Give the child
its goal, context, scope, constraints, acceptance criteria, and required evidence.
Choose a short descriptive name. Leave model selection to the Smart Router hook;
do not change the parent model or select a different provider. Never simulate
delegation by launching another CLI from the shell.

For read-only work, explicitly prohibit edits. For implementation, transfer the
complete edit, test, and repair loop. Include this boundary in every assignment:
execute the assignment directly, do not delegate again, and return the complete
result and decisive evidence to this parent. Preserve user authority and tool
permissions; never change credentials, sandbox, or concurrency limits.

Do not repeat the child's investigation. Split work only when the outputs can be
accepted independently. Collect the result through Copilot's native task and
follow-up tools before answering; a spawn acknowledgement is not a completed
assignment. If a background task is still working, wait using the native task
mechanism rather than inventing a completion or polling its output files.

Check each requested result against the returned evidence. Request a focused
amendment from the same child for missing coverage. Synthesize the final response
only after acceptance. Report actual errors and unresolved evidence honestly.
