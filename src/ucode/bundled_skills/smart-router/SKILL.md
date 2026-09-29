---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed session.
allowed-tools: Bash(__UG_EXECUTABLE__ __UG_LAUNCHER__ --enable-smart-routing), Bash(__UG_EXECUTABLE__ __UG_LAUNCHER__ --disable-smart-routing)
---

# Smart Router

For `on` or `off`, run exactly one matching command:

```text
__UG_EXECUTABLE__ __UG_LAUNCHER__ --enable-smart-routing
__UG_EXECUTABLE__ __UG_LAUNCHER__ --disable-smart-routing
```

With no argument, explain that only `on` and `off` are accepted. Do not edit the state file.
This affects subsequent subagent model selection in the current session, not the root model or
first prompt. Return the command's result.
