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
The shared recovery check covers both adaptive and enabled thinking, preserving any token budget.
Claude may then receive `safeguards: Extra inputs are not permitted`. Recovery must remove
only that field in the next native attempt and end with a non-empty HTTP 200. The checks
inspect recorded traffic without modifying or replaying requests.

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

## Smart-routing CUJ

`test_cuj4_smart_routing.py` uses its own read-only workspace with managed smart routing enabled.
It runs the original routed/explicit-model journeys and five
`SMART_ROUTER_CONFIG_VERSION` cases using the shared version constants; each launches Claude
and Codex once.
The independent expectation table is:

| Selector | First prompt routed | Orchestrator context | Child on first prompt | Explicitly requested child |
| --- | --- | --- | --- | --- |
| `first_prompt_and_subagent_no_orch_v0` | Yes | No | No | Yes, routed |
| `subagent_only_v0` | No | No | No | Yes, routed |
| `subagent_only_v1` | No | No | No | Yes, routed |
| `subagent_orch_v0` | No | Yes | No | Yes, routed |
| `subagent_orch_v1` | No | Yes | No | Yes, routed |

First prompts explicitly forbid delegation to isolate first-prompt routing. Assertions require
the expected presence/absence of a prompt-correlated router request, successful inference on
the routed or configured default model, completed native file-task evidence, and no child session.
Orchestrator presence means its activation context reached the real gateway inference input,
not that an assistant echoed it or a skill merely existed on disk.
Task inference must contain the exact task/routed prompt and tools, excluding Claude title
requests and parent continuations from child-inference checks.
Parent and child requests use the same verified thinking-display recovery as CUJ3:
only known display/safeguards rejections followed by native removal of the rejected field
and a final non-empty 200 are accepted. Model, prompt, budget, and effort must remain unchanged.
Each preset session then explicitly requests one child for a separate hidden-value
file task. Assertions require a native child transcript containing the value, the completed
parent answer, a correlated spawn-routing decision, and successful child inference on the
router's selected model. This tests requested delegation, not automatic orchestrator delegation.
Codex requires a matching completed turn and final parent answer, not just child notifications.
Offline evidence regressions do not establish a live CUJ pass.
Selector cases have separate TUI artifact names, and the session environment is restored afterward.
Each preset also runs public `ug revert` in cleanup, including after a failed assertion, so
interactive launches' OS-managed settings cannot contaminate the next preset's configuration.
The existing Claude explicit-model precedence case remains skipped; the routing-disabled case
still requires a separately preconfigured workspace.

With the same live prerequisites:

```bash
uv run --with pexpect==4.9.0 --with pyte==0.8.2 pytest \
  --confcutdir=tests/e2e_cuj tests/e2e_cuj/test_cuj4_smart_routing.py \
  -k test_smart_router_config_version -v
```
