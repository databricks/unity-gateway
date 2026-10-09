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
Claude's built-in Default action is not a catalog entry; banners use catalog display names,
while gateway inference checks require the exact service/alias.
Compatible `ug_e2e.other_models.claude_decoy` and `codex_decoy` must be discoverable in their
own schema but absent from the scoped picker. Claude discovery aliases must be enabled;
Kimi uses `anthropic-aigw-<8-character SHA-256 prefix>-<service FQN>`.

Ten independently configured cases cover picker discovery, default launches, and explicit models.
Picker cases dismiss the menu without changing selection, then complete a task on the default.
Separate cases cover bare `ug`, `ug claude`, and `ug codex` TUI first tasks and Claude print/Codex
exec defaults. Each additional compatible model gets its own headless task case.
Success requires a completed native task and successful recorded gateway inference for the
expected service/alias, not generated settings or catalog caches. Defaults omit model overrides;
expected answers are withheld from prompts. Claude headless results also require nonzero output
tokens in `modelUsage`; Codex joins the completed answer to its client-selected turn model.
Claude transcript model IDs name the backing model, not the service. TUI tasks reject unexpected
permission prompts. These checks do not prove the gateway's backing destination. Only live passes
establish coverage. Claude 2.1.290 may receive a 400 rejecting `thinking.display: "updates"`;
CUJ3 accepts it only if the next task request removes that field, changes nothing else in the
payload, and receives a non-empty HTTP 200. Other failures remain test failures.

The test class selects the CUJ3 workspace, `https://dbc-bbdd5508-648e.cloud.databricks.com`.
The shared `cuj` fixture supplies its authenticated SDK client and isolated local session;
workspace configuration remains read-only. Each case cleans up with public `ug revert` before
the next case configures, including after failures. Set `UG_CUJ_SP_CLIENT_ID` and
`UG_CUJ_SP_CLIENT_SECRET` before a live run.

CI discovers each test file and runs it on its own runner with both pinned agents. No catalog-specific workflow or workspace secret is needed. Locally,
install ug, both agents, and the Databricks CLI on a clean POSIX host, then run:

```bash
uv run --with pexpect==4.9.0 --with pyte==0.8.2 pytest \
  --confcutdir=tests/e2e_cuj tests/e2e_cuj/test_cuj3_models.py -v
```

MCP/skills discovery remains separate.

Collection only (no authentication or inference):

```bash
uv run pytest -c tests/e2e_cuj/pytest.ini --confcutdir=tests/e2e_cuj \
  --collect-only tests/e2e_cuj
```
