@AGENTS.md

# Claude Code Notes

Use `AGENTS.md` as the shared source of repository instructions.

Put Claude-specific workflow notes here only when they are not useful to other agents.

Use absolute executable paths for subprocess calls, hooks, skills, and generated commands,
rather than bare names such as `ug` or `python`. Bind commands to the intended installation
at launch so a different `PATH` in an agent's shell cannot select another version. For Python
entry points, use the launching `sys.executable` with `-m`; preserve its virtualenv path instead
of resolving it to the underlying system Python. Quote executable paths in shell commands and
keep subprocess commands as argument lists.
