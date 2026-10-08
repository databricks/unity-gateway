# Claude Smart Router inference recipe

When Claude starts with `ENABLE_SMART_ROUTING_V2=1` or
`ENABLE_SMART_ROUTING_SUBAGENT_ONLY=1`, Unity Gateway initializes
`SMART_ROUTER_RECIPE_LOCAL` to `SMART_ROUTER_NAME`, or `task_v3` when that variable is unset or
blank. The value is stored in the session's `UCODE_SESSION_ENV_FILE`, shared by the parent,
children, hooks, and Smart Router controls. Every launch gets its own file.

The initial recipe is merged into `CLAUDE_CODE_EXTRA_BODY`. Existing JSON fields are retained;
invalid extra-body JSON produces an actionable launch error. `/smart-router off` atomically
sets the session recipe to the shared `SMART_ROUTER_DISABLED` constant (`DISABLED`).
`/smart-router on` restores the launch's configured recipe.

Claude 2.1.286 caches the file supplied through `--settings`: replacing its extra-body value did
not update subsequent native requests. UG therefore starts a session-owned loopback forwarder
and passes its URL only in launch settings. The forwarder reads the authoritative session value
for each `/v1/messages` request and replaces `smart_router_recipe_name` before forwarding to the
configured endpoint. Parent and child requests use the same forwarder. Other body fields and
authentication headers are preserved, including gzip request bodies and streaming responses.
No request bodies or credentials are logged. The listener is closed when Claude exits.

The process environment contains the launch-time value; the session file is authoritative after
a toggle. UG does not mutate shared Claude settings to update a session's recipe. Explicit-model
and headless launches still carry recipe metadata when a routing flag is enabled, even though
they bypass UG's model-selection wrapper. Never-enabled sessions start no recipe forwarder and
receive no UG-injected extra-body field. Missing or corrupt session state blocks inference
instead of silently forwarding a stale recipe.

## Managed endpoint limitation

This draft rejects recipe-enabled launches when an OS-managed settings file enforces
`ANTHROPIC_BASE_URL`: that scope overrides the loopback URL, so request updates would otherwise
silently stop working. UG normally mirrors its configured endpoint into that scope. Supporting
those normal configurations requires a session-aware native extra-body mechanism or a change
to endpoint ownership; this draft does not modify shared policy files to bypass that precedence.
The four payload criteria are therefore validated only when the session endpoint override is
allowed. This is a rollout blocker, not a completed managed-settings implementation.

## Manual verification

Configure Claude for the workspace you intend to test, then run:

```bash
export ENABLE_SMART_ROUTING_SUBAGENT_ONLY=1
export ENABLE_SMART_ROUTER_ORCHESTRATOR=0
unset ENABLE_SMART_ROUTING_V2
unset SMART_ROUTER_NAME
uv run ug claude
```

In the same session, submit a main-agent task and explicitly request a child task. Invoke
`/smart-router off`, repeat both tasks, invoke `/smart-router on`, and repeat once more. Inspect
gateway request evidence: the three phases should carry `task_v3`, `DISABLED`, and `task_v3`
in `smart_router_recipe_name`, for both parent and child inferences. The turn performing the
toggle can begin under the previous value; requests submitted after the toggle completes use
the updated value.

Repeat with `SMART_ROUTER_NAME` set to a supported custom recipe. Open a second Claude session
and confirm that toggling the first leaves the second's request values unchanged. Also repeat
with `ENABLE_SMART_ROUTING_V2=1` and `ENABLE_SMART_ROUTING_SUBAGENT_ONLY=0` for full routing.
Finally launch with both flags set to `0` and confirm UG does not add the body field. No manual
`CLAUDE_CODE_EXTRA_BODY` or `SMART_ROUTER_RECIPE_LOCAL` export is needed.

Component tests run with `uv run pytest tests/test_claude_recipe_payload.py -v`. A native
Claude 2.1.286 probe used a local fixture endpoint and the production UG toggle command to
verify parent/child on/off/on propagation with both the default and a custom recipe. Live
gateway backend behavior requires the request-evidence check above.
