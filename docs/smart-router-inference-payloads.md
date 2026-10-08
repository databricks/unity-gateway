# Smart Router inference payloads

Unity Gateway marks opted-in Codex inference context with an exact developer-message marker:

```text
<ug-routing-state>{"smart_router_recipe_name":"task_v3"}</ug-routing-state>
```

The recipe value comes from `SMART_ROUTER_NAME`, whose default remains `task_v3`. Turning Smart
Router off in the same session emits a later marker with
`{"smart_router_recipe_name":null}`. A session that was never launched with Smart Router does not
install Unity Gateway's metadata hooks and emits no marker.

Backend extraction must scan developer messages in inference order and use the latest applicable
marker whose tag and JSON field match exactly. An explicit `null` clears a previously selected
recipe and overrides the legacy `Databricks-Smart-Router-Recipe` header. Malformed, partial, or
differently tagged text is not a routing-state marker.

## Draft limitations

- Reused Codex subagents still need native-harness evidence that each turn receives refreshed
  developer context after an on/off transition.
- Incremental WebSocket inference coverage is still required for the full-routing launch path.
- Component tests establish hook composition and hook output. They do not establish complete
  per-request propagation through the native Codex harness and AI Gateway backend.

## Claude Code structured body draft

The stacked Claude Code draft provides helpers that merge `smart_router_recipe_name` into the
JSON object stored in `CLAUDE_CODE_EXTRA_BODY`. When routing is on, the value comes from
`SMART_ROUTER_NAME` (default `task_v3`). Turning routing off writes
`{"smart_router_recipe_name":"DISABLED"}`, replacing any previous recipe. Turning routing back
on restores the configured recipe. Existing body fields, environment variables, and unrelated
settings are preserved. Malformed existing JSON is left untouched.

The atomic update helper accepts only an explicit path to an existing, session-owned watched
settings file. It does not discover or write shared user settings, OS-managed settings, or an
arbitrary caller-supplied `--settings` file. Automatic launch and on/off-toggle wiring is
intentionally not enabled in this draft.

Before this path can be enabled, native Claude Code testing must establish concurrent-session
isolation, reload timing, settings precedence, and supported launch modes. A green helper-level
test does not establish per-request propagation through Claude Code.
