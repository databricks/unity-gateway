# CUJ3 model-only fixtures

This standalone layer adapts the full CUJ3 provisioner and managed config for
model discovery only. It owns seven UC model services and, when missing, the
`ug_e2e` catalog and its `models` and `other_models` schemas. It does not require,
inspect, create, or configure MCP services, skills, Databricks Apps, connections,
agent installations, credentials, grants, or managed-config publication. Later
MCP/skill CUJ stacks are separate layers, not prerequisites for this layer.

| Schema | Services | Purpose |
| --- | --- | --- |
| `ug_e2e.models` | `gpt_luna` | Codex default; GPT family |
| `ug_e2e.models` | `claude_haiku`, `claude_sonnet` | Claude family; Sonnet is the Claude default |
| `ug_e2e.models` | `kimi` | Shared Claude/Codex compatible model |
| `ug_e2e.models` | `gemini_flash` | Gemini family; intended exclusion from both agents |
| `ug_e2e.other_models` | `claude_decoy`, `codex_decoy` | Compatible, accessible, out-of-scope decoys |

Service identities are stable aliases, not inferred source model names. `gpt_luna`
replaces the earlier `gpt_terra` choice. The provisioner maps every leaf to its
schema explicitly; inventory ordering does not determine scope.

## Prerequisites and setup order

1. Use Python 3.12+ and select the exact dedicated CUJ3 HTTPS workspace origin.
   No SDK, CLI profile, or implicit workspace/authentication fallback is used.
2. An operator must explicitly select seven existing, canonical `system.ai.<model>`
   registered-model FQNs in that workspace/region, including both decoys. These are
   backing UC models, not friendly model-service aliases. Live GET checks verified
   that `system.ai.gpt-6-luna` is not a registered model, while
   `system.ai.databricks-gpt-6-luna` exists; supply the latter for that chosen version.
   Before any writes, both `--validate` and `--apply` GET every unique source at
   `/api/2.1/unity-catalog/models/{source}` and require its `full_name` to exactly
   match the explicit source. Missing or mismatched sources fail without writes;
   the script never resolves aliases or chooses a replacement. Choose the intended
   GPT, Claude Haiku, Claude Sonnet, Kimi, and Gemini Flash families for their aliases.
   Haiku, Sonnet, Kimi, and the Claude decoy must support Claude's wire protocol;
   GPT Luna, Kimi, and the Codex decoy must support Codex's wire protocol.
   The script never guesses versions, discovers sources, or supplies defaults.
   Decoys may reuse compatible in-scope sources; their UC identities remain distinct.
3. For `--validate` or `--apply` only, supply a workspace bearer through
   `DATABRICKS_BEARER` (or an explicitly chosen
   `--bearer-env` variable) using your approved secret mechanism. Never put tokens
   in arguments, source, shell history, logs, or captured output; disable shell
   tracing. The script prints only resource names, source names, and sanitized
   errors, never tokens or server response bodies. Redirects are refused.
4. The provisioning principal needs permission to read the existing inventory,
   create missing catalog/schemas/services, and use the chosen source models.
   If it cannot create the catalog, have an authorized operator create it first.
   Separately grant the CUJ execution principal `USE_CATALOG`, `USE_SCHEMA`, and
   the required model-service invocation/discovery permissions for **both** schemas.
   Decoys must be accessible so exclusion proves scope filtering, not missing grants.
5. Run the default offline plan (no bearer or network needed), review the workspace,
   all seven mappings, permissions, and request format. Use `--validate` for an
   authenticated read-only inventory check; missing resources cause a nonzero exit.
   After review, explicitly add `--apply` to create missing resources. Rerun
   `--validate`: it should report zero missing resources and exit successfully.
6. Review `cuj3-managed-config.json` before an authorized administrator publishes
   it separately. Publishing changes workspace policy; this script never does it.
   The config points both agents to `ug_e2e.models`, defaults the agent to Claude,
   defaults Claude to Sonnet and Codex to GPT Luna, disables
   smart routing and tracing, and contains no MCP or skill scope. Those omissions
   do not promise to remove unrelated pre-existing local MCP/skill configuration.

From the repository root, without needing a bearer for the default offline plan:

```bash
python3 fixtures/cuj/models/provision.py \
  --workspace "$UG_CUJ3_WORKSPACE" \
  --bearer-env DATABRICKS_BEARER \
  --gpt-luna-source "$GPT_LUNA_SOURCE" \
  --claude-haiku-source "$CLAUDE_HAIKU_SOURCE" \
  --claude-sonnet-source "$CLAUDE_SONNET_SOURCE" \
  --kimi-source "$KIMI_SOURCE" \
  --gemini-flash-source "$GEMINI_FLASH_SOURCE" \
  --claude-decoy-source "$CLAUDE_DECOY_SOURCE" \
  --codex-decoy-source "$CODEX_DECOY_SOURCE"
```

Each source variable must contain a concrete `system.ai.<model>` FQN. The default
plan validates only argument syntax and prints the intended inventory; it performs
no network requests, reads no bearer, and cannot determine what already exists.
`--validate` requires a bearer, performs GETs, validates every existing service,
and fails if any resource is missing, without writing. `--validate` and `--apply`
are mutually exclusive. `--apply` performs the full preflight **before any POST**, then
creates only missing resources in catalog → schemas → services order and reads back
all seven services for validation. Existing routing must exactly match the expected
single destination, destination type, and source-model reference. Mismatches abort;
there is no overwrite, update, delete, or cleanup path. Concurrent create conflicts
and partial failures stop the run; inspect the inventory with `--validate`
before retrying. This is not an atomic transaction, and created resources persist.

## Compatibility and validation boundary

The intended agent-specific catalog contract is:

| Agent | Exact `ug_e2e.models` leaves | Default |
| --- | --- | --- |
| Claude | `claude_haiku`, `claude_sonnet`, `kimi` | `claude_sonnet` |
| Codex | `gpt_luna`, `kimi` | `gpt_luna` |

`gemini_flash` is intentionally excluded from both catalogs. This is an expected
contract, **not proven by provisioning**. Both agents share the same schema pointer;
API-specific compatibility filtering must produce these sets, not a fixture-side
static allowlist. The compatible decoy for each agent must be discoverable under
`ug_e2e.other_models` but absent from the `ug_e2e.models` catalog.

Discovery is not validated inference: a catalog entry, generated configuration,
picker row, or successful startup alone does not prove a callable model. Each
expected in-scope model must complete a real file task with native model identity
evidence in the live CUJ. Provisioning validation checks routing only and sends no
inference requests; it cannot prove source availability or agent compatibility.

The model-service API uses `/api/2.1/unity-catalog/model-services`, a schema
`parent`, and canonical snake_case fields matching the local Python SDK sample
`experimental/eng-ai-governance/api-examples/python-sdk/model_service.py` in Universe.
Creation sets `pay_per_token_config.model = models/system.ai.<model>` and
`traffic_percentage = 100`. Usage tracking is always on; its removed/reserved
configuration field is omitted, as specified by
`managed-catalog/atlas/modules/model_service/model_service.proto` in Universe.
Readback validation requires exactly one destination with the selected source name,
pay-per-token destination type, and matching model target. It accepts server-added
metadata and defaults: `is_deleted = false`, empty fallback routing, or omitted
traffic percentage (the SDK documents that a single primary receives all traffic).
Deleted/disabled destinations, explicit traffic percentages other than 100,
fallback destinations, and source/type/target mismatches fail without overwriting
existing services. Usage-tracking metadata is not part of routing validation.
Actual workspace API availability, source existence, and
Claude/Codex inference wire compatibility are **not verified by offline checks**.
Operator confirmation against the intended workspace is required before apply;
service metadata validation alone cannot prove completed inference.

After separate publication and agent setup, root-owned CUJ tests should verify each
agent's compatible in-scope default/extra models completes inference with the
expected service identity, its compatible decoy is independently discoverable and
callable outside the managed scope, and managed discovery excludes that decoy.
This layer neither runs inference nor installs/configures either agent. Tests, CI,
and broader CUJ documentation remain outside this directory's ownership.

Suggested focused local checks (no workspace or bearer needed):

```bash
uv run ruff check fixtures/cuj/models/provision.py
uv run ruff format --check fixtures/cuj/models/provision.py
python3 fixtures/cuj/models/provision.py --help
```
