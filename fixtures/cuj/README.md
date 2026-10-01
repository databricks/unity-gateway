# CUJ2 and CUJ3 Unity Catalog fixtures

This directory defines persistent, workspace-specific fixtures for the named-resource
CUJ2 and schema-pointer CUJ3 tests. `mcp_app/` is the backing Databricks App. The
three skill bundles are uploaded as Unity Catalog skills. `provision.py` validates
or creates the catalog resources; it never updates or deletes an existing resource.

## Inventory

| Schema | Resources | Purpose |
| --- | --- | --- |
| `ug_e2e.models` | `claude_sonnet`, `claude_extra`, `codex_primary`, `codex_extra` | Two callable models for each agent |
| `ug_e2e.other_models` | `claude_decoy`, `codex_decoy` | Accessible, out-of-scope models |
| `ug_e2e.tools` | `fixture_reader` (`read_fixture`), `fixture_metadata` (`describe_fixture`) | In-scope MCP calls |
| `ug_e2e.other_tools` | `fixture_decoy` (`decoy_status`) | Accessible, out-of-scope MCP |
| `ug_e2e.skills` | `fixture-summary`, `fixture-audit` | In-scope downloaded skills |
| `ug_e2e.other_skills` | `fixture-decoy` | Accessible, out-of-scope skill |

The ticket's example skill identifiers use underscores. UC skill creation rejects
underscores, so the IDs and `SKILL.md` frontmatter use hyphens.

## App and connection prerequisite

Deploy `mcp_app/` to the CUJ3 workspace as a Databricks App, then make a
schema-scoped HTTP connection to its `/mcp` endpoint using OAuth M2M. Create
`ug_e2e` and its schemas first if the connection lives in `ug_e2e.tools`; an
initial `--apply` run can create those schemas and will stop at the missing
connection. Once the connection exists, rerun the provisioner. The App
service principal needs `CAN_USE` on the App. Supply that connection's three-part
name to the provisioner. See the first-party
`ai-gateway/scripts/mcp_service_sentinel_probe/src/probe_mcp_service.py` flow
for the App permissions and connection API body. Keep the OAuth client secret in
the connection credential store. Rotate it before expiry; the sentinel's
short-lived probe secret is unsuitable for a persistent fixture.

The provisioner reads an explicit `DATABRICKS_BEARER` and `--workspace`. Use the
same CUJ service principal that CI uses, or grant that principal `USE_CATALOG`,
`USE_SCHEMA`, and the needed resource permissions before CI runs. This also lets
the test prove decoys are visible to that principal even though managed discovery
must exclude them. Do not put the workspace URL or credentials in source.

Specify six existing, working `system.ai` source models. Choose Claude-compatible
sources for the three Claude leaves and Codex-compatible sources for the three
Codex leaves. The source flags are explicit because availability differs by
workspace and region. A validation run has no write effect:

```bash
python3 fixtures/cuj/provision.py \
  --workspace "$UG_CUJ3_WORKSPACE" \
  --connection ug_e2e.tools.fixture_app_connection \
  --claude-sonnet-source system.ai.CLAUDE_DEFAULT \
  --claude-extra-source system.ai.CLAUDE_EXTRA \
  --codex-primary-source system.ai.CODEX_DEFAULT \
  --codex-extra-source system.ai.CODEX_EXTRA \
  --claude-decoy-source system.ai.CLAUDE_DECOY \
  --codex-decoy-source system.ai.CODEX_DECOY
```

After reviewing the selected workspace, model sources, and connection, add
`--apply` to create missing catalog resources. Rerun without `--apply` to validate
the resulting inventory. The provisioner refuses an existing model service or
MCP service with different routing or selectors, and an unfinalized skill.

## Managed config for CUJ3

Publish this config in the dedicated CUJ3 workspace after fixture validation:

```json
{
  "spec_version": 1,
  "default_agent": "CODING_AGENT_CLAUDE_CODE",
  "enabled_agents": [
    {
      "agent": "CODING_AGENT_CLAUDE_CODE",
      "config": {
        "models": { "unity_catalog_location": "ug_e2e.models" },
        "default_models": {
          "default_model": "ug_e2e.models.claude_sonnet",
          "default_sonnet_model": "ug_e2e.models.claude_sonnet"
        },
        "smart_routing": { "enabled": false },
        "tracing": { "enabled": false }
      }
    },
    {
      "agent": "CODING_AGENT_CODEX",
      "config": {
        "models": { "unity_catalog_location": "ug_e2e.models" },
        "default_models": { "default_model": "ug_e2e.models.codex_primary" },
        "smart_routing": { "enabled": false },
        "tracing": { "enabled": false }
      }
    }
  ],
  "mcp_servers": { "unity_catalog_location": "ug_e2e.tools" },
  "skills": { "unity_catalog_location": "ug_e2e.skills" }
}
```

The CI workflow should select the CUJ3 workspace URL from its repository secret
and reuse the CUJ service-principal credentials. The CUJ3 test must invoke each
discovered in-scope model, verify completed inference with its model identity,
and call both MCP tools through both agents. Inventory and listing checks alone
are insufficient.
