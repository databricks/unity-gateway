# MCP registration CUJ

The dedicated workspace must publish `mcp_servers.unity_catalog_location = ug_e2e.tools`
for both agents. Fixtures are provisioned separately; this suite does not modify them.

| Service | Read-only tool |
| --- | --- |
| `ug_e2e.tools.fixture_reader` | `read_fixture(run_id)` |
| `ug_e2e.tools.fixture_metadata` | `describe_fixture(run_id)` |

Each journey configures installed UG, opens the real agent MCP inventory, and requires
connected servers with their tools loaded. Claude opens each server's details and tools
view; Codex uses `/mcp verbose`. Both then call the two tools with a fresh run ID and return
their receipts. Expected receipts are computed locally and withheld from the prompt.
Terminal actions, rendered screens, and native transcripts are recorded by the harness.

CI runs one lane per agent using the existing `UG_CUJ3_WORKSPACE` and CUJ SP secrets.
The lanes run after catalog discovery, even if it fails, and share its workspace
concurrency lock. Each lane uploads terminal and native-transcript evidence.

Run on a clean POSIX host with the CUJ SP credentials and dedicated workspace:

```bash
python3.12 scripts/run_integration.py --suite e2e-cuj \
  --ug-version checkout --claude-version 2.1.280 --codex-version 0.154.0 \
  --workspace "$CATALOG_DISCOVERY_WORKSPACE" \
  -- -m 'managed and mcp_registration and workspace_isolated'
```

Only live passes establish coverage. Out-of-scope exclusion and UG status/list checks
remain separate follow-ups. Skill storage and provisioning are not part of this suite.
