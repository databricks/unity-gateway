# PR 1400: Claude child continuation

Validated on 2026-11-03 against
`https://eng-ml-inference.staging.cloud.databricks.com`, with Claude Code **2.1.314**,
`SMART_ROUTER_CONFIG_VERSION=subagent_orch_v0`, and checkout `1548892230b0`.
Both runs used isolated homes and mount namespaces; host managed settings were
excluded, and configure wrote only inside the namespaces. Gateway calls were real.

## Manual TUI check — passed

Launched the checkout through `ug configure --agents claude --workspace <workspace>`
and `ug claude`. Completed onboarding and project trust through the native TUI.
The workspace selected `system.ai.claude-opus-5` for parent and child.

1. Asked the parent to spawn exactly one child, read `ug-resume-first.txt`, retain
   its contents, and return only `READY`, keeping the value private from the parent.
2. After completion, asked the parent to resume that child with `SendMessage` and
   return the remembered contents without reading files again.

Transcript evidence:

| Observation | Result |
| --- | --- |
| Agent/Task calls across parent and child | 1 |
| Child transcripts | 1 |
| Original child ID | `a4a853d56239bb48c` |
| `SendMessage.to` | `a4a853d56239bb48c` |
| Child Read calls across both turns | 1 |
| Initial child result | `READY` |
| Synthetic file value | `memory-db98e1b53792` |
| Resumed child and final parent answer | `memory-db98e1b53792` |

The parent's recorded API request shapes contained the orchestrator Agent
description and the `To continue a previously spawned agent, use SendMessage`
note. The parent did not receive the synthetic value until the resumed child replied.

## Installed-package regression — passed

The integration runner built and installed the checkout wheel and pinned Claude
2.1.314 through the internal package proxies. Ran only
`test_smart_router_claude_resumes_same_subagent_with_send_message`:
**1 passed, 185 deselected, no skips**, in 40.10 seconds.

The checked-in managed fixture selected `system.ai.claude-sonnet-4-6`. Both file
tasks completed in the same child transcript, with one Agent call and one
SendMessage addressed to that child's ID, `abd184b8988a9ce0e`.

Also passed: five native Claude mod tests on 2.1.314, 98 integration contract
checks, and all three lint gates (ruff check, ruff format check, ty).

These runs establish explicit spawn and continuation on this version and
workspace. They do not establish automatic delegation quality, behavior on every
newer Claude release, or a full integration-suite pass. Raw run artifacts live
under `/tmp` and are disposable; this report retains the selected evidence.
