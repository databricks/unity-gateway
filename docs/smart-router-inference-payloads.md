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
