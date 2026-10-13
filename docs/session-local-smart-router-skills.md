# Session-local Smart Router skill visibility

Deleting `~/.claude/skills/smart-router` or `smart-router-orchestrator` affects
every Claude session using that home. Claude watches skill directories, so an
already running routed session can lose its controls when an unrelated launch
removes those files. PR #1111 proposes the same shared-file deletion for Codex.

The Claude deletion calls and their now-unused helpers have been reverted.
Routing setup failure still disables routing through launch-only environment
overrides. Skills remain installed and may appear in unrouted launches until a
session-local visibility override is implemented.

## Claude

Pass this JSON through the existing launch settings composition:

```json
{
  "skillOverrides": {
    "smart-router": "off",
    "smart-router-orchestrator": "off"
  }
}
```

`claude --settings '<json>'` applies this to that invocation. `"off"` hides a
skill from both the model's listing and slash-command autocomplete, and rejects
explicit invocation. It works for the personal skills UG installs; plugin skills
use a different mechanism. Merge only these names into existing overrides and
retain unrelated caller settings. Do not write shared settings or edit SKILL.md.

Sources: [skill visibility](https://code.claude.com/docs/en/skills#override-skill-visibility-from-settings)
and [settings reference](https://code.claude.com/docs/en/settings-reference#skilloverrides).

## Codex

Codex supports disabling a skill by its absolute SKILL.md path. Supply the
`skills.config` array through a CLI override instead of writing config.toml:

```sh
codex --no-daemon -c 'skills.config=[{path="/absolute/home/.codex/skills/smart-router/SKILL.md",enabled=false},{path="/absolute/home/.codex/skills/smart-router-orchestrator/SKILL.md",enabled=false}]'
```

This example assumes no other `skills.config` entries. An implementation must
retain unrelated existing entries because the override replaces the array.
Resolve paths from the configured home rather than hardcoding them.

UG's full-routing path starts a dedicated app server; apply the override to that
server, which discovers skills, rather than just its remote TUI. Direct launches
can reuse Codex's shared daemon. Native testing below establishes isolation for
separate app servers; reuse of a shared daemon and per-thread overrides remain
unverified. `--no-daemon` is the direct-launch candidate for ensuring a separate
server, but its TUI behavior was not exercised by this investigation.
Do not use `skills/config/write` as a session-local substitute: it is a
configuration-write API.

Sources: [skill configuration](https://developers.openai.com/codex/skills/),
[CLI config overrides](https://developers.openai.com/codex/config-advanced/),
and [app-server skill APIs](https://developers.openai.com/codex/app-server/).

## Evidence (2026-10-13)

- Claude Code 2.1.296: two concurrent native stream-JSON sessions shared one
  config directory containing both skills. The normal session advertised both
  in its `system/init.slash_commands`; the session with `skillOverrides` off
  advertised neither. The first session remained responsive. Both completed a
  local immediate plugin command with zero inference turns/output tokens.
  Skill files survived and no shared settings.json was written.
- Codex 0.162.1: two concurrent native stdio app servers shared one CODEX_HOME.
  `skills/list` reported both skills enabled in the first server and disabled
  in the server started with the CLI override. A forced reload in the first
  still reported both enabled. No config.toml was written.
- The focused component suite passed 432 tests, including preservation of skill
  bundle contents and the first session's controls during an unrouted Claude
  launch and a routing-setup fallback. Ruff check, Ruff format check, and ty
  passed. These checks do not establish live gateway or concurrent TUI coverage.

The session-local replacement is investigated here, not implemented. Minimum
supported agent versions, shared-daemon behavior, remote-TUI pickers, resume,
and live skill invocation need validation before shipping it.
