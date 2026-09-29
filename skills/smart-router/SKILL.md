---
name: smart-router
description: Enable or disable Unity Gateway subagent model routing for the current smart-routed Claude or Codex session.
allowed-tools: Bash(ug claude --enable-smart-routing), Bash(ug claude --disable-smart-routing), Bash(ug codex --enable-smart-routing), Bash(ug codex --disable-smart-routing)
metadata:
  version: "1.0.0"
---

# Smart Router

- In Claude Code, run exactly `ug claude --enable-smart-routing` for `on` or
  `ug claude --disable-smart-routing` for `off`.
- In Codex, run exactly `ug codex --enable-smart-routing` for `on` or
  `ug codex --disable-smart-routing` for `off`.

With no argument, explain that only `on` and `off` are accepted. Do not edit the state file.
This affects subsequent subagent model selection in the current session, not the root model or
first prompt. Return the command's result.
