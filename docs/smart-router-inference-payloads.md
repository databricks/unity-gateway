# Claude Code Smart Router inference payload draft

The Claude Code draft provides helpers that merge `smart_router_recipe_name` into the
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
