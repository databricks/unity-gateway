# Catalog discovery CUJ

The dedicated workspace must already publish a CodingAgentConfig enabling both agents
with `ug_e2e.models` as their model source. Claude is the default agent; its default and
Sonnet-family model are `claude_sonnet`, and Codex defaults to `gpt_luna`. Smart routing
and tracing are disabled. Tests neither provision nor validate fixture definitions.

| Service under `ug_e2e.models` | Claude Code | Codex |
| --- | --- | --- |
| `gpt_luna` | Included | Included; default |
| `claude_haiku` | Included | Excluded |
| `claude_sonnet` | Included; default | Excluded |
| `kimi` | Included | Included |
| `gemini_flash` | Excluded | Excluded |

Independent gateway catalogs, Codex's native model/list, and exact TUI picker inventories must agree.
Claude's built-in Default action is not a catalog entry; banners use catalog display names,
while gateway inference checks require the exact service/alias.
Compatible `ug_e2e.other_models.claude_decoy` and `codex_decoy` must be discoverable in their
own schema but absent from the scoped picker. Claude's decoy catalog includes both decoys.
Claude discovery aliases must be enabled;
Kimi and GPT Luna use `anthropic-aigw-<8-character SHA-256 prefix>-<service FQN>`.

Eleven independently configured model cases cover picker discovery, default launches, and explicit models.
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
Retries match system context and session metadata to exclude concurrent parent traffic.

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

CUJ1, CUJ2 and CUJ3 share Desktop setup that seeds the same personal profile and metadata in an
isolated Linux profile path. CUJ1 and CUJ2's configuration cases read their dedicated workspaces'
published CodingAgentConfig without `UCODE_MANAGED_CONFIG_STUB`. They enable this profile path
before their existing installed `ug configure`, then compare the entire profile and metadata
JSON with the expected structure and values. This includes model labels, helper arguments and
refresh settings, headers, discovery settings, and CUJ1's OTLP fields. The profile uses either
the published Databricks-hosted Claude models or the Anthropic provider service and selected model.
CUJ2 expects its model ID as the label. Actual and expected profile JSON are retained in
`desktop-profile.json` for failures.
Both also require the personal profile's bytes to survive and metadata to retain its entry.
They do not launch Desktop or execute the credential helper.

CUJ3's existing Claude picker journey also configures beside a personal Desktop profile and checks
every profile/metadata byte after its existing configure call. Its scoped
aliases use names such as `ug_e2e.models.claude_haiku`, which Desktop's Claude-family filter does
not accept, so existing files must remain unchanged and UG must not save selection state.
Successful Desktop profiles remain covered by CUJ1/CUJ2. No extra CUJ test or configure is added.

CUJ2's Claude and Codex MPS inference cases ask for a short greeting. Each requires a completed
native parent turn matching that prompt, a nonempty answer, the expected provider/model and
its paired HTTP 200 response. Random request IDs correlate traffic without requiring models
to repeat them. File, skill, MCP and subagent tasks keep their expected-value assertions.

## Smart-routing CUJ

`test_cuj4_smart_routing.py` uses its own read-only workspace with managed smart routing enabled.
It runs the managed-default and explicit-model journeys and six
`SMART_ROUTER_CONFIG_VERSION` presets using the shared version constants. With no selector,
managed smart routing leaves the first prompt unrouted: the admin's default model completes the
file task, no `routes:select` request is sent, and the Smart Router Orchestrator context reaches
that inference request. Each preset runs as independent tests so a slow live subagent call
cannot fail unrelated assertions: one first-prompt-then-explicit-subagent journey per agent
(12 cases, with Claude ending in the native background-task wait) and one Claude-only status
band/hint row/toggle journey per preset (6 cases, which needs no delegation or background-task
wait).
The CUJ derives routing and orchestration assertions from each agent's resolved preset flags.
Component tests independently verify the preset values. The resulting expectations are:

| Selector | First prompt routed | Orchestrator context | Child on first prompt | Explicitly requested child |
| --- | --- | --- | --- | --- |
| `first_prompt_and_subagent_no_orch_v0` | Yes | No | No | Yes, routed |
| `subagent_only_v0` | No | No | No | Yes, routed |
| `subagent_only_v1` | No | No | No | Yes, routed |
| `subagent_orch_v0` | No | Yes | No | Yes, routed |
| `subagent_orch_v1` | No | Yes | No | Yes, routed |
| `subagent_orch_v0_claude_only` | No | Claude only | No | Yes for both; only Claude routed |

First prompts explicitly forbid delegation to isolate first-prompt routing. Assertions require
the expected presence/absence of a prompt-correlated router request, successful inference on
the routed or configured default model, completed native file-task evidence, and no child session.
Orchestrator presence means its activation context reached the real gateway inference input,
not that an assistant echoed it or a skill merely existed on disk.
Task inference must contain the exact task/routed prompt and tools, excluding Claude title
requests and parent continuations from child-inference checks. Only Claude child requests
after the routing checkpoint may append one transport newline to that exact prompt.
Codex 0.162.1 encrypts child instructions, so those requests instead match the routed
task name to the child message's recipient. The child's hidden-value and model checks remain.
Parent and child requests use the same verified thinking-display recovery as CUJ3:
only known display/safeguards rejections followed by native removal of the rejected field
and a final non-empty 200 are accepted. Model, prompt, budget, and effort must remain unchanged.
Each preset session then explicitly requests one child for a separate hidden-value
file task. Assertions require a new native child transcript containing the value,
a correlated spawn-routing decision and successful child inference on the
router's selected model when routing is enabled. Disabled routing requires no router
requests, new routing decisions, recipe metadata, or orchestrator context. Claude's parent
and child inference payloads carry the launch recipe in `smart_router_recipe_name`
without a recipe header; Codex retains the recipe header. This tests requested delegation, not automatic orchestrator delegation.
Claude's recipe is compared with the recorded routing request's selector rather than
a hardcoded default recipe name.
The wait uses the child's answer directly; parent-answer reconstruction is reserved for first-prompt tasks.
Offline checks reject parent-only answers and accept child answers before the parent replies.
Offline evidence regressions do not establish a live CUJ pass.
Selector cases have separate TUI artifact names, and the session environment is restored afterward.
Each Claude preset session checks the visible ON state and routing scope,
then OFF and restored ON after `/smart-router off` and `on`.
These checks read the terminal's rendered text, ignoring styling and trailing UI controls.
Offline cases reject stale states, wrong scope, and echoed command confirmations.
Claude sessions start in auto mode; unless a routed first prompt switches the session to Haiku
(which leaves it in manual mode), its classifier requests through the recording proxy trigger
Claude's informational auto-mode classifier billing notice over the transcript. The CUJ terminal
waits for that exact notice to render stably, presses Enter (continue), and observes dismissal
before continuing. Later occurrences are handled the same way; any other dialog still fails.
After verifying the delegated task, Claude sessions also wait in the native `/tasks` view
until no background work remains, including a child resumed after its initial answer.
Only then does the test close the task view and submit `/exit`.
Each preset also runs public `ug revert` in cleanup, including after a failed assertion, so
interactive launches' OS-managed settings cannot contaminate the next preset's configuration.
The Claude and Codex explicit-model precedence cases both run; the routing-disabled case
still requires a separately preconfigured workspace.

With the same live prerequisites:

```bash
uv run --with pexpect==4.9.0 --with pyte==0.8.2 pytest \
  --confcutdir=tests/e2e_cuj tests/e2e_cuj/test_cuj4_smart_routing.py \
  -k test_smart_router_preset -v
```
