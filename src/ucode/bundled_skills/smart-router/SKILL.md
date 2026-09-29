---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed session.
allowed-tools: Bash(__UG_EXECUTABLE__ __UG_LAUNCHER__ --enable-smart-routing), Bash(__UG_EXECUTABLE__ __UG_LAUNCHER__ --disable-smart-routing)
---

# Smart Router

Interpret the invocation's first argument as `on` or `off`, then run exactly one matching command:

```text
__UG_EXECUTABLE__ __UG_LAUNCHER__ --enable-smart-routing
__UG_EXECUTABLE__ __UG_LAUNCHER__ --disable-smart-routing
```

With no argument, explain that the accepted arguments are `on` and `off`; do not run a command.
Use this only for the current smart-routed __UG_LAUNCHER__ session. To start a routed session,
run `__UG_EXECUTABLE__ __UG_LAUNCHER__ --enable-smart-routing` from outside the session.
Return the command's result to the user. Do not edit the session-state file directly. This switch
affects only subsequent subagent model selection; it does not change the current/root model or
reroute the first prompt.
