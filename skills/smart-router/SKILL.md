---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session.
allowed-tools: Bash(ug smart-router on), Bash(ug smart-router off)
metadata:
  version: "1.0.0"
---

# Smart Router

- For `on`, run exactly `ug smart-router on`.
- For `off`, run exactly `ug smart-router off`.

With no argument, explain that only `on` and `off` are accepted. Do not edit the state file.
This affects subsequent subagent model selection in the current session, not the root model or
first prompt. Return the command's result.
