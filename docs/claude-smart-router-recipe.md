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

Earlier direct Claude 2.1.286 probes observed watched user `settings.json` updates in
both parent and child requests. The stronger two-session test in the dependent native
settings draft exposed a stale first parent request after a one-second wait, followed
by updated child and parent requests. Hot reload is possible, but reliable propagation
before a toggle reports completion remains unresolved. See `claude-session-settings.md`
for reproducible tests, the timing failure, and the remaining production adapter work.

The same probe replacing a file supplied through `--settings` kept sending `task_v3`.
Exporting an environment variable from a hook subprocess also cannot mutate its parent
Claude process's environment.

UG currently supplies startup settings. A watched settings source private to each
session still needs to be wired before off/on changes reach native inference requests.
A shared user settings file would allow sessions to affect each other. The original
on/off/on payload success criteria remain incomplete. Recipe metadata uses no new
proxy and leaves the configured gateway endpoint unchanged.

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
