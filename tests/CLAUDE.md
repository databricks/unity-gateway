# Test instructions for Claude Code

Read and follow [AGENTS.md](AGENTS.md) in this directory and the coverage matrix
in [README.md](README.md) before changing tests. Those are the shared instructions
for adding, modifying and removing tests; do not maintain a different policy here.

In particular, `integration/` uses the real installed ug CLI, selected real agent
versions, and the real e2e workspace. No mocks, monkeypatching, internal application
calls, fake binaries/services, fabricated ug state, or test-only production
behavior. Do not hide failures with skips, xfails, retries or weaker assertions.
Update the matrix when coverage changes and distinguish automated coverage from
gaps and checks that were not executed.

The one carve-out is the `managed_fixture` marker: those tests inject the admin
CodingAgentConfig through the built-in `UCODE_MANAGED_CONFIG_STUB` hook or through
`ug configure --file` so the real `ug configure` path can be exercised across managed-config
shapes the live workspace does not publish. `ug configure --file` persists for later plain
launches, so launch journeys can use it too, not just configure-only ones; existing stub-based
launch journeys stay on the stub for now (migrating them is an optional follow-up). That controls
only the admin-authored INPUT; the gateway, agent binaries, ug internals, and ug state stay real,
and the config fetch/wire contract itself stays covered by the un-stubbed `managed` tests. Faking
any of those remains banned.
