# Catalog discovery CUJ

The dedicated workspace must already publish a CodingAgentConfig enabling both agents
with `ug_e2e.models` as their model source. Claude is the default agent; its default and
Sonnet-family model are `claude_sonnet`, and Codex defaults to `gpt_luna`. Smart routing
and tracing are disabled. Tests neither provision nor validate fixture definitions.

| Service under `ug_e2e.models` | Claude Code | Codex |
| --- | --- | --- |
| `gpt_luna` | Excluded | Included; default |
| `claude_haiku` | Included | Excluded |
| `claude_sonnet` | Included; default | Excluded |
| `kimi` | Included | Included |
| `gemini_flash` | Excluded | Excluded |

Independent gateway catalogs, Codex's native model/list, and exact TUI picker inventories must agree.
Compatible `ug_e2e.other_models.claude_decoy` and `codex_decoy` must be discoverable in their
own schema but absent from the scoped picker. Claude discovery aliases must be enabled;
Kimi uses `anthropic-aigw-<8-character SHA-256 prefix>-<service FQN>`.

Ten independently configured cases cover picker discovery, default launches, and explicit models.
Picker cases dismiss the menu without changing selection, then complete a task on the default.
Separate cases cover bare `ug`, `ug claude`, and `ug codex` TUI first tasks and Claude print/Codex
exec defaults. Each additional compatible model gets its own headless task case.
Success depends on native behavior, not generated settings, managed-config JSON, or catalog caches.
Defaults omit model overrides; expected answers are withheld from prompts. Claude headless results
require the requested service/alias in `modelUsage` with nonzero output tokens; TUI cases check the
selected default in the native banner and a completed assistant answer. Claude transcript model IDs
name the backing model, not the service. Codex evidence joins the completed answer to its
client-selected turn model. None proves the gateway's backing destination. Only live passes
establish coverage.

The tests in `test_catalog_discovery.py` subclass Lilly's `BaseCujTest`.
`WORKSPACE_URL` comes from the explicit runner workspace; the SDK authenticates with
`UG_CUJ_SP_CLIENT_ID` and `UG_CUJ_SP_CLIENT_SECRET`. Existing integration helpers
provide fresh installed-agent sessions, real TUI driving, and transcript evidence.

CI runs a serialized Claude/Codex matrix using `UG_CUJ3_WORKSPACE` and the CUJ SP
secrets. Job concurrency prevents simultaneous runs against that workspace. Both
lanes block full/live runs. Run on a clean host/container with the two SP environment
variables set and an explicit dedicated workspace:

```bash
python3.12 scripts/run_integration.py --suite e2e-cuj \
  --ug-version checkout --claude-version 2.1.280 --codex-version 0.154.0 \
  --workspace "$CATALOG_DISCOVERY_WORKSPACE" \
  -- -m 'managed and catalog_discovery and workspace_isolated'
```

MCP/skills discovery remains separate.

Collection only (no authentication or inference):

```bash
uv run pytest -c tests/e2e_cuj/pytest.ini --confcutdir=tests/e2e_cuj \
  --collect-only tests/e2e_cuj
```
