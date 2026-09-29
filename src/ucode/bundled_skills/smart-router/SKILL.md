---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session.
allowed-tools: Bash(__UG_EXECUTABLE__ smart-router on), Bash(__UG_EXECUTABLE__ smart-router off)
---

# Smart Router

Interpret the invocation's first argument as `on` or `off`, then run exactly one matching command:

```text
__UG_EXECUTABLE__ smart-router on
__UG_EXECUTABLE__ smart-router off
```

With no argument, explain that the accepted arguments are `on` and `off`; do not run a command.
Return the command's result to the user. Do not edit the session-state file directly. This switch
affects only subsequent subagent model selection; it does not change the current/root model or
reroute the first prompt.
