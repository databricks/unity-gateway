[Watch the native offline savings recording](offline-savings.mp4) (21.55 seconds, 241 KB).

Claude Code 2.1.322 runs the savings-enabled `subagent_orch_savings_v0` preset.
The native router selects Sonnet for a child that reads `invoice-note.txt` and
verifies `50 - 8 = 42`. The prompt shows estimated savings of **$0.23 (57%)**;
`/smart-router off` hides the estimate and `/smart-router on` restores it.

**OFFLINE FIXTURE:** Messages responses, token usage, routing selection and endpoint
rates are prepared local inputs in a network namespace with loopback only. This
does not measure actual billing or live-gateway savings. The native Agent and Read
tools, routing CLI, accounting, display and session controls execute normally.

The MP4 replays captured native terminal text in monochrome at its original timing,
with onboarding trimmed and brief viewing holds. T3 preview capture was unavailable
because no automation host was connected. Raw transcripts and profile data remain
private; [provenance](provenance.json) records the runtime, source and media hashes.

The recording uses source commit `1721714aa99d86659735bd729830d5e5b97279c0`,
which passed 468 selected component, integration-contract and native-client cases,
plus Ruff, formatting and type checks. This documentation commit leaves those
validated source bytes unchanged.
