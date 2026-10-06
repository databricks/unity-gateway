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

The test class selects the CUJ3 workspace, `https://dbc-bbdd5508-648e.cloud.databricks.com`.
The shared `cuj` fixture supplies its authenticated SDK client and isolated local session;
workspace configuration remains read-only. Set `UG_CUJ_SP_CLIENT_ID` and
`UG_CUJ_SP_CLIENT_SECRET` before a live run.

CI discovers these tests through the shared `dedicated-cuj` job, which installs both
pinned agents. No catalog-specific workflow or workspace secret is needed. Locally,
install ug, both agents, and the Databricks CLI on a clean POSIX host, then run:

```bash
uv run --with pexpect==4.9.0 --with pyte==0.8.2 pytest \
  --confcutdir=tests/e2e_cuj tests/e2e_cuj/test_catalog_discovery.py -v
```

MCP/skills discovery remains separate.

Collection only (no authentication or inference):

```bash
uv run pytest -c tests/e2e_cuj/pytest.ini --confcutdir=tests/e2e_cuj \
  --collect-only tests/e2e_cuj
```
