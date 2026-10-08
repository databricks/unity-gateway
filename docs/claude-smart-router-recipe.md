# Claude Smart Router inference recipe

When Claude starts with `ENABLE_SMART_ROUTING_V2=1` or
`ENABLE_SMART_ROUTING_SUBAGENT_ONLY=1`, Unity Gateway initializes
`SMART_ROUTER_RECIPE_LOCAL` to `SMART_ROUTER_NAME`, or `task_v3` when unset or blank.
Each launch has its own `UCODE_SESSION_ENV_FILE`, shared with its hooks and controls.

The initial recipe is merged into native `CLAUDE_CODE_EXTRA_BODY`. Existing body fields
are retained; malformed or non-object JSON fails launch with an actionable error.
Explicit-model and headless launches also carry the metadata when a routing flag is enabled.
Never-enabled launches receive no UG-injected recipe field.

`/smart-router off` updates the desired session recipe to `SMART_ROUTER_DISABLED`
(`DISABLED`) and updates the desired extra body in the same atomic session-file write.
`/smart-router on` restores the configured recipe. Another session's state is unchanged.
These state changes do not yet refresh the running native client's request payload.

## Native reload evidence and remaining work

The native settings draft now verifies an in-memory alternative: `apply_flag_settings`
(the TypeScript SDK's `Query.applyFlagSettings`). After an acknowledged update to
`env.CLAUDE_CODE_EXTRA_BODY`, main-agent and native Agent child requests used the new
value immediately in the probe; another session kept its own value. That test created
no settings files and used no watcher delay. See `claude-session-settings.md` for the
SDK source, reproducible tests, precedence, and update semantics.

The control API requires streaming input. This Smart Router draft still supplies startup
settings and writes desired toggle state to its session file; it does not yet connect
toggles to the native control API. UG's ordinary interactive PTY launch has no verified
connection to that API. Completing that integration is required before this draft meets
the original off/on payload criteria. The configured gateway endpoint is unchanged.

## Startup verification

Use an already configured Claude workspace:

```bash
export ENABLE_SMART_ROUTING_SUBAGENT_ONLY=1
unset ENABLE_SMART_ROUTING_V2
unset SMART_ROUTER_NAME
uv run ug claude
```

Main and child inference requests should initially contain
`{"smart_router_recipe_name":"task_v3"}`. Repeat with `SMART_ROUTER_NAME` set to a
supported recipe, then with `ENABLE_SMART_ROUTING_V2=1`. With both routing flags unset
or set to `0`, UG should add no recipe field. Do not manually export
`SMART_ROUTER_RECIPE_LOCAL` or `CLAUDE_CODE_EXTRA_BODY` for these startup checks.

Off/on currently verifies desired session state only. Do not treat it as evidence of
changed outbound requests until native reload wiring is implemented.

Run component checks with:

```bash
uv run pytest tests/test_claude_recipe_payload.py -v
```
