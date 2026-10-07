# MCP registration and listing CUJ

The dedicated workspace must publish `mcp_servers.unity_catalog_location = ug_e2e.tools`
for both agents. Fixtures are provisioned separately; this suite does not modify them.

| Service | Read-only tool |
| --- | --- |
| `ug_e2e.tools.fixture_reader` | `read_fixture(run_id)` |
| `ug_e2e.tools.fixture_metadata` | `describe_fixture(run_id)` |

The two TUI journeys configure installed UG, open the real agent MCP inventory, and require
connected servers with their tools loaded. Claude opens each server's details and tools
view; Codex uses `/mcp verbose`. Both then call the two tools with a fresh run ID and return
their receipts. Expected receipts are computed locally and withheld from the prompt.
Terminal actions, rendered screens, and native transcripts are recorded by the harness.

Both inventories must omit `ug_e2e.other_tools`, including the accessible
`fixture_decoy` service and its `decoy_status` tool. Claude traverses the server menu
until it wraps; Codex includes rendered scrollback from `/mcp verbose`.

The command journey checks that `ug mcp list` shows both managed fixture rows for `claude, codex`
with aggregate `connected` status and no `ug_e2e.other_tools` servers. This combines Claude's
health check with Codex's `enabled` registration; Codex connectivity is verified by its TUI/tool
journey, not the aggregate label. Local-server counts are not asserted.

Run on a clean disposable POSIX host with `ug`, `claude`, `codex`, and `databricks` on `PATH`.
Set `UG_CUJ_SP_CLIENT_ID` and `UG_CUJ_SP_CLIENT_SECRET`, install `pexpect==4.9.0` and
`pyte==0.8.2`, and use a host without machine-wide agent settings. The test uses
the dedicated workspace URL declared on `TestMcpRegistration`; no workspace URL environment
variable is required.

```bash
uv run --with pexpect==4.9.0 --with pyte==0.8.2 pytest \
  --confcutdir=tests/e2e_cuj tests/e2e_cuj \
  -m 'managed and mcp_registration and workspace_isolated' -v
```

CI discovers these tests in the shared `dedicated-cuj` job alongside catalog discovery.
Only live passes establish coverage. `ug status` is not part of this CUJ.
Skill storage and provisioning are not part of this suite.
