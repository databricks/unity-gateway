# Cowork subagent hook probe

This standalone plugin tests whether Cowork runs a hook before subagent tool
calls. It returns `{"systemMessage":"hi"}` from a synchronous `PreToolUse`
command matching `Agent` or `Task`, requesting a visible `hi` before each call.
It uses only the POSIX shell's built-in `printf` and changes no inputs or permissions.

From the repository root, package it for Cowork:

```sh
cd scripts/cowork-spawn-hi
python3 -m zipfile -c /tmp/cowork-spawn-hi.plugin .claude-plugin hooks README.md
```

Upload `/tmp/cowork-spawn-hi.plugin` through Cowork's plugin upload flow, or add
the plugin to a test organization's managed plugins. Enable it, start a new
Cowork task, and ask:

> Use two subagents to independently compute 17 * 19 and 23 * 29.

Confirm that children actually spawn and look for `hi` before each call.
A missing message is inconclusive: Cowork may not load the hook, may use another
tool name, or may hide system messages. This does not establish coverage of
internal spawns that bypass these tools, or support for model overrides.

`ug configure` does not install this probe. It includes no skill, HTTP call,
credentials, or routing behavior. Disable or remove the plugin after testing.
