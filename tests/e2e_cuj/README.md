# Catalog discovery CUJ

The dedicated workspace must already publish a CodingAgentConfig enabling Claude Code, Codex and
Copilot CLI with `ug_e2e.models` as their model source. Claude is the default agent; its default
and Sonnet-family model are `claude_sonnet`, Codex defaults to `gpt_luna`, and Copilot defaults to
`claude_sonnet`. Smart routing
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

## Model sources in CUJ9

`test_cuj9_model_sources.py` uses
`https://dbc-93fdd3cb-4b13.cloud.databricks.com`, workspace `7474659962444308`.
Its 14 cases cover native discovery and default inference for Claude Code, Codex,
and Copilot CLI, explicit inference on every expanded model, and one routed
Claude child. Smart routing is enabled only for Claude Code. Codex and Copilot
tasks must send no routing requests.

Copilot explicit inference on each source model is currently skipped (gap): the gateway
rejects the provider-service `gpt-6-luna` request with a 400 on `mlflow/v1/responses`.
Re-enable it once that route is fixed (AIGTWY-5588).

| Agent | Explicit MPS deep link | Explicit model service | Wildcard |
| --- | --- | --- | --- |
| Claude Code | `ug_e2e.providers.anthropic.claude-haiku-5-5` | `ug_e2e.models.claude_sonnet` | `ug_e2e.other_models.*` includes both aliases |
| Codex | `ug_e2e.providers.openai.gpt-6-luna` | `ug_e2e.models.gpt_luna` | `ug_e2e.other_models.*` includes only `codex_decoy` |
| Copilot CLI | `ug_e2e.providers.anthropic.claude-haiku-5-5` | `system.ai.gpt-6-luna` (hosted) | `ug_e2e.providers.openai.*` adds the qualified GPT model |

Each source contributes distinct models, so wildcard expansion cannot hide a
missing explicit source. Claude and Codex default to their custom model services;
Copilot defaults to the hosted GPT service. Copilot requires names such as
`gpt-6-luna`; aliases such as `gpt_luna` are incompatible with its model classifier.
Custom-model inference is covered by Claude and Codex.

Claude and Codex discovery cases compare exact native picker inventories; Codex
also checks its app-server `model/list`. Copilot checks the active per-session
registry's model IDs and wire providers without archiving its credentials. This
is registry and completed-task coverage, not a Copilot picker assertion. Every
explicit-model task requires a completed native answer containing a hidden file
value and successful recorded inference on the exact model. The routing case uses
the TUI request recorder to pair `routes:select` with an HTTP 200 response selecting
an offered source model. It also requires completed child and parent answers.

The workspace directly publishes CodingAgentConfig
`coding-agent-configs/d55f1e06-54bf-42cd-9b2c-19db77da968f`, created on 2026-11-16,
with the sources and defaults above. Claude is the default agent, tracing is off
for all three agents, and Copilot VS Code integration is off. The suite reads and
validates this live configuration; it does not load a local configuration artifact.
The CUJ service principal needs access to the workspace and named services.
Tests never change the published configuration, services, or grants.

Direct API probes on 2026-11-16 verified inference with the Anthropic catalog's
`anthropic-aigw-e6dfbcac-ug_e2e.other_models.codex_decoy` alias when the request
includes `x-databricks-use-coding-agent-mode: true`, which UG sets automatically.
11 of the 14 cases are active; the three `test_sources_copilot_inference_on_each_source_model`
cases are skipped (gap) pending AIGTWY-5588. The qualified OpenAI provider
model succeeds at `/ai-gateway/codex/v1/responses`, but Copilot's configured
`/ai-gateway/mlflow/v1/responses` path rejects its native API type.

Use a clean disposable POSIX runner and the normal CUJ service-principal
variables. Install the checkout's `ug`, Databricks CLI `1.17.0`, Claude Code
`2.1.290`, Codex `0.156.0`, and Copilot CLI `1.0.91` on PATH. CI installs the
pinned Copilot CLI only for this automatically discovered shard.

```bash
uv run --with pexpect==4.9.0 --with pyte==0.8.2 pytest \
  --confcutdir=tests/e2e_cuj tests/e2e_cuj/test_cuj9_model_sources.py -v
```

Collection and offline checks establish suite structure only. No live CUJ9 pass
is claimed until the published configuration and agent journeys have been
validated together. Direct API probes do not establish native agent or TUI coverage.

CUJ1, CUJ2 and CUJ3 share Desktop setup that seeds the same personal profile and metadata in an
isolated Linux profile path. CUJ1 and CUJ2's configuration cases read their dedicated workspaces'
published CodingAgentConfig without `UCODE_MANAGED_CONFIG_STUB`. They enable this profile path
before their existing installed `ug configure`, then compare the entire profile and metadata
JSON with the expected structure and values. This includes model labels, helper arguments and
refresh settings, headers, discovery settings, and CUJ1's OTLP fields. The profile uses either
the published Databricks-hosted Claude models or the Anthropic provider service and selected model.
CUJ2 requires `Claude Haiku 5.5` for its `claude-haiku-5-5` Anthropic target, including when discovery
echoes that target ID as the display name. Actual and expected profile JSON are retained in
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

CUJ2's Copilot CLI entry uses the Anthropic MPS, `ug_e2e.providers.anthropic`, with VS Code
integration on. Its headless file task, with no `--model`, must start on `claude-haiku-5-5`,
send the provider header on every Messages request that carries the task, and get a non-empty
HTTP 200 for each.

CUJ3's Copilot case reads Copilot's live registry: exactly the four Claude-visible scoped services,
under their full names on the Anthropic route (Copilot picks wires by model name, and these names
fall back to it), with no Gemini and no decoys. Its TUI task completes on `claude_sonnet`.

A separate CUJ2 Claude TUI case checks default Model Provider Service support with the
first-prompt/subagent routing preset. It checks the managed MPS header and paired HTTP 200 on
both route-selection requests, plus completed parent and child turns.

CUJ2's OpenAI MPS, `ug_e2e.providers.openai`, must expose `gpt-6-luna`, with every target on both
Responses and Chat Completions native APIs; its workspace's Codex default must match. CUJ2 and
CUJ7 accept extra targets on both shared provider services; CUJ9 accepts them too and expects
Copilot's `ug_e2e.providers.openai.*` source to list every live OpenAI target.
CUJ1 and CUJ6 use `system.ai.gpt-6-luna` for the Codex Luna selection. Provision the CUJ1
and CUJ6 phase A model lists, plus the CUJ6 phase C model list and default, before running
these cases. Tests only validate the published fixtures; they never update them.

## Smart-routing CUJ

`test_cuj4_smart_routing.py` uses its own read-only workspace with managed smart routing enabled.
Its Auto toggle journey returns from a manual model through `/model auto`.
The full-screen alias assertion is temporarily commented out with a TODO: Claude
2.1.290 exposes internal Auto aliases in some headers and in `/status`. Model-selection
and routing-indicator assertions remain enabled.
It runs the managed-default and explicit-model journeys and seven
`SMART_ROUTER_CONFIG_VERSION` presets using the shared version constants. With no selector,
managed smart routing leaves the first prompt unrouted: the admin's default model completes the
file task, no `routes:select` request is sent, and the Smart Router Orchestrator context reaches
that inference request. Each preset runs as independent tests so a slow live subagent call
cannot fail unrelated assertions: one first-prompt-then-explicit-subagent journey per agent
(14 cases, including the Auto preset, with Claude ending in the native background-task wait) and one Claude-only status
band/hint row/toggle journey per preset (6 cases, which needs no delegation or background-task
wait).
The CUJ derives routing and orchestration assertions from each agent's resolved preset flags.
Its Codex Auto case uses the shared `subagent_auto_v0` selector, checks Auto above
the regular models in one native picker, chooses an alternate main model by its display
name and one of its supported reasoning efforts, and requires a completed file task
with paired gateway inference on that model and effort and no first-prompt routing request.
It then switches to a manual model and back to Auto, requiring both choosers to reopen.
An additional Claude-only selector journey overrides the version with
`subagent_auto_v0_claude_only`, checks startup Auto with the managed default model,
selects a regular model and verifies the band and footer show OFF, then returns
to Auto and verifies both show ON. It selects the first available alternative with Down and Enter,
and checks that saving updates
the prompt hint without adding a transcript notice. The managed replacement
picker contains one Auto row; only regular model rows supply alternate inference targets.
The journey completes a real main-model file task with paired gateway request/response
evidence, dismisses a pending choice with Escape, and reopens on the saved model.
Saving that same model skips the cache warning. After restart, it serves a second file task.
Managed settings stay byte-identical across model selections. Launch may retarget only
the gateway URL and auth helper host to the recording proxy; all other configured settings
must remain identical. The existing main/subagent journey also includes Auto
and verifies its correlated routing decision, real model IDs, and completed answers.
The selector journey has its own class-scoped user session so its saved main-model
preference cannot change later preset journeys. Its native evidence baseline is captured
before Claude starts, including sessions whose transcript begins with model commands.
Component tests independently verify the preset values. The resulting expectations are:

| Selector | First prompt routed | Orchestrator context | Child on first prompt | Explicitly requested child |
| --- | --- | --- | --- | --- |
| `first_prompt_and_subagent_no_orch_v0` | Yes | No | No | Yes, routed |
| `subagent_only_v0` | No | No | No | Yes, routed |
| `subagent_only_v1` | No | No | No | Yes, routed |
| `subagent_orch_v0` | No | Yes | No | Yes, routed |
| `subagent_orch_v1` | No | Yes | No | Yes, routed |
| `subagent_orch_v0_claude_only` | No | Claude only | No | Yes for both; only Claude routed |
| `subagent_auto_v0_claude_only` | No | No | No | Yes for both; only Claude routed |

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
and child inference payloads carry the launch recipe in `smart_router_recipe_name`.
Codex carries the recipe in `x-codex-turn-metadata`, with `smart_router_state: "on"`
for `/root` and an `agent_name` ending in `__sr_on` for the child. Claude omits
the legacy recipe header; Codex retains it with the same recipe and omits the
payload recipe field. Each agent's recipe must match the recorded routing request's
selector rather than a hardcoded default. Disabled Codex presets omit both routing
fields from native metadata.
This tests requested delegation, not automatic orchestrator delegation.
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
Each Claude preset then opens `/smart-router logs`, requires a nonzero session routing
count, closes the pane, and submits `/exit`. This includes presets that hide inline
subagent model details; the live log-pane checks have not been run locally.
These Claude launches pass `--disallowedTools ScheduleWakeup,CronCreate`: a reminder the model
schedules to check on its subagent would outlive the finished child and hold `/exit` open.
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

CUJ 7's existing Claude discovery case reconfigures with the Auto version after its
ordinary discovery launch, removing UG's earlier replacement picker. The Auto launch
keeps its aliases in launch settings. It saves an Auto selection on the unmanaged
workspace, completes a main-model file task, then restarts with routing disabled.
The restart must complete another task on the same real model with paired successful
HTTP evidence and no router request. It never seeds Auto aliases in shared settings.


CUJ7 includes independent Claude and Codex saved-provider smart-routing journeys. Its existing
before/after checks require no published CodingAgentConfig. Each journey configures its agent
with the shared Anthropic or OpenAI provider, then launches the real TUI with only
`--enable-smart-routing`. A routed native child reads a hidden local file; both child and parent
must return its value. Recorded requests require the saved provider header on routing and
inference, successful paired responses, and a selected offered model applied to the child.
These fixtures have small catalogs. The allow-all Bedrock startup and oversized-catalog cases
remain separate staging validation and are not established by these CUJs.
CI pins Codex 0.165.0 for CUJ7, matching CUJ4's routing-capable version.
