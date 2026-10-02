# CUJ3 model-only fixtures

This layer provisions seven UC model services and, when missing, the `ug_e2e`
catalog and its `models` and `other_models` schemas. Apps, connections, MCP, and
skills are not prerequisites. Agent installation, credentials, grants, and
managed-config publication are separate operator setup.

| Schema | Services | Purpose |
| --- | --- | --- |
| `ug_e2e.models` | `gpt_luna` | Codex default; GPT family |
| `ug_e2e.models` | `claude_haiku`, `claude_sonnet` | Claude family; Sonnet is the Claude default |
| `ug_e2e.models` | `kimi` | Shared Claude/Codex compatible model |
| `ug_e2e.models` | `gemini_flash` | Gemini family; intended exclusion from both agents |
| `ug_e2e.other_models` | `claude_decoy`, `codex_decoy` | Compatible, accessible, out-of-scope decoys |

Service identities are stable aliases, not source model names. Each leaf maps
explicitly to its schema; inventory ordering does not determine scope.

## Prerequisites and setup order

1. Use Python 3.12+ and select the exact dedicated CUJ3 HTTPS workspace origin.
   No SDK, CLI profile, or implicit workspace/authentication fallback is used.
2. An operator must explicitly select seven existing, canonical `system.ai.<model>`
   registered-model FQNs in that workspace/region, including both decoys, not
   friendly model-service aliases. Both `--validate` and `--apply` GET every unique source at
   `/api/2.1/unity-catalog/models/{source}` and require its `full_name` to exactly
   match the explicit source before any writes. Missing or mismatched sources fail;
   the script never resolves aliases, discovers/replaces sources, or guesses versions/defaults.
   Choose GPT, Claude Haiku, Claude Sonnet, Kimi, and Gemini Flash sources for their aliases.
   Haiku, Sonnet, Kimi, and the Claude decoy must support Claude's wire protocol;
   GPT Luna, Kimi, and the Codex decoy must support Codex's wire protocol.
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
5. Review the offline plan's workspace, seven mappings, permissions, and request
   format. Run `--validate`, then explicitly use `--apply` for missing resources.
   Rerun `--validate` and require zero missing resources and a successful exit.
6. An authorized administrator separately reviews and publishes
   `fixtures/cuj-3/managed-config.json`; this script never changes workspace policy.
   It points both agents to `ug_e2e.models`, selects Claude as the default agent,
   Sonnet for Claude and GPT Luna for Codex, disables smart routing/tracing,
   and has no MCP/skill scope.
   Those omissions do not remove unrelated pre-existing local MCP/skill config.

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

Each source variable is a concrete `system.ai.<model>` FQN. The default plan checks
syntax and prints inventory without network access or reading a bearer; it cannot
check existing resources. `--validate` performs authenticated read-only GETs and
fails on missing resources or invalid services. It is mutually exclusive with
`--apply`, which completes full source/inventory/routing preflight **before any POST**,
creates only missing catalog → schemas → services, and reads back all seven services.
Mismatches abort: there is no overwrite, update, delete, or cleanup path. Concurrent
create conflicts and partial failures stop the non-atomic run; created resources
persist. Inspect with `--validate` before retrying.

## Compatibility and validation boundary

The intended agent-specific catalog contract is:

| Agent | Exact `ug_e2e.models` leaves | Default |
| --- | --- | --- |
| Claude | `claude_haiku`, `claude_sonnet`, `kimi` | `claude_sonnet` |
| Codex | `gpt_luna`, `kimi` | `gpt_luna` |

`gemini_flash` is excluded from both catalogs. API-specific filtering of the shared
schema pointer must produce these exact sets, without a fixture-side allowlist.
Each compatible decoy must be discoverable under `ug_e2e.other_models` but absent
from the managed catalog.

The API uses `/api/2.1/unity-catalog/model-services`, a schema `parent`, and
canonical snake_case fields from Universe's Python SDK model-service sample.
Creation sets `pay_per_token_config.model = models/system.ai.<model>` and
`traffic_percentage = 100`; usage tracking is always on and its removed/reserved
configuration field is omitted. Preflight/readback require one destination with
the selected source name, pay-per-token type, and matching model target. Server
metadata/defaults (`is_deleted = false`, empty fallback routing, omitted traffic
percentage for a single primary) are accepted. Deleted/disabled destinations,
explicit traffic other than 100, fallbacks, or source/type/target mismatches fail.
Usage-tracking metadata is not a routing check.

Offline checks do not verify workspace API availability, source existence, or wire
compatibility; confirm these in the intended workspace before apply. Provisioning
checks source metadata and routing, not inference availability or compatibility.
After publication and agent setup, live CUJs must compare discovery/pickers and
complete file tasks on every in-scope compatible model. Claude evidence is the
response-reported model; Codex evidence is the client-selected model. Neither proves
the executed gateway backing destination. Discovery, config, or startup alone is
insufficient; tests and CI remain outside this fixture-only layer.

Suggested focused local checks (no workspace or bearer needed):

```bash
uv run ruff check fixtures/cuj/models/provision.py
uv run ruff format --check fixtures/cuj/models/provision.py
python3 fixtures/cuj/models/provision.py --help
```
