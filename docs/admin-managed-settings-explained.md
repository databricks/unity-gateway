# About admin-managed agent settings

When `ug configure --global` runs on a workspace that publishes an agent configuration, it
rebuilds each supported agent's machine-wide files from that configuration. This page calls that a
managed run. It explains why the design looks the way it does. For the facts, see
[Admin-managed agent settings reference](admin-managed-settings-reference.md).

## A published agent configuration makes a run managed

`ug` doesn't ask who started the run or why. An MDM policy, a provisioning script, a fleet tool,
and an administrator at a terminal all get the same result. What decides the outcome is the
workspace: when it publishes an agent configuration, that configuration describes the
machine-wide files completely, so `--global` builds them from it. Without one, there is nothing to
build from, and `--global` merges its settings into the existing files the way it always has.

## One root run writes every machine-wide file

These files are owned by root. If every launch through `ug` could update them, each update would cost
a developer a `sudo` prompt, and two tools that both wrote the file would undo each other's changes.
So the managed run is the only writer. After it runs once, it leaves a marker file, and no other `ug` run
on that machine writes the files. Developers never see a password prompt, and the files change only
on IT's schedule.

The cost is latency. An admin's change reaches a machine on its next managed run, so that run's
schedule sets how fast the fleet follows the agent configuration.

## The run rebuilds the files instead of editing them

Each run builds the files from nothing. The result depends only on the agent configuration and the
`ug` version. A setting the admin removes is gone from the next file. A `--dry-run` preview of a
draft shows exactly what a real machine gets, and a `ug` downgrade leaves nothing behind to clean
up.

The rebuild discards anything else in the files, including a hand edit made with `sudo`. That
trade-off is why the design assumes that no other tool writes these files.

## The files hold no per-user values

One machine file serves every developer on the machine, so it can't name one developer's
Databricks CLI profile, PAT, or OAuth client. The sign-in helper is `ug auth-token --host <workspace>`.
It runs as whichever developer starts the agent, and it reads that developer's saved choices.

Models follow the same rule. An explicit `models.model_services` list is the same for everyone, so
the managed run writes it. A Unity Catalog location or a model provider service has to be resolved with
the developer's own access, so `ug claude` and `ug codex` resolve it at launch. A bare `claude` or
`codex` reads only the machine files, so it misses those models, tracing, and smart routing.

## Unity Gateway reserves keys by exception

The ownership tables list the keys Unity Gateway needs: the gateway endpoint, the sign-in helper,
the headers, and the keys its features own while they are on. Every other key passes through. An
agent release can add a setting, and admins can use it the same day, with no `ug` release.

The risk runs the other way. A new setting that routes requests around the gateway also passes
through until a `ug` release reserves it. Someone has to read the agents' release notes for
provider and sign-in settings.

Unity Gateway defaults, such as `ENABLE_PROMPT_CACHING_1H`, are enforced like everything else in
Claude Code's managed file. An admin can change a default. A developer can't.

## An agent can need more than one file

Each agent's ownership table has one section per machine-wide file. Codex is the current case with
two. It has one place to enforce settings, `requirements.toml`, and it accepts a fixed set of keys.
Ordinary `config.toml` keys go in `/etc/codex/config.toml`, the lowest config layer, where
developers can override them. The two files use some of the same keys with different meanings: in
`config.toml`, `mcp_servers` defines servers, and in `requirements.toml` it is an allowlist. So the
agent configuration has two Codex fields.

Unity Gateway locks the gateway provider in `requirements.toml`. A locked provider entry replaces
anything a launch passes with `-c`, so the per-launch routing headers travel in environment
variables that the provider names in `env_http_headers`. The lock first appears in Codex 0.156.0.

`/etc/codex/managed_config.toml` would also work, but Codex calls it legacy, and its values outrank
the settings that `ug codex` passes per launch.

## Unpublishing leaves the files in place

When the workspace stops publishing the agent configuration that a machine used, the managed run exits 5 and
changes nothing. An empty answer from the server, or coding agent configs being switched off, then
can't silently remove the gateway, sign-in, and policy from every machine at once. Removing the
settings takes a deliberate `ug revert` as root.

Removing one agent from `enabled_agents` is different, because the published configuration still
says what it wants. The next run deletes that agent's files.

## Some features override admin settings

A few features need keys that admins might also set, and while the feature is on, the feature wins.
Letting the feature win keeps it working, and each case has a cost:

- With `tracing` on, Unity Gateway's `otelHeadersHelper` replaces the admin's. Claude Code applies
  one helper to every OTEL signal, so the two exports can't run together.
- With `smart_routing` on, the admin's `env.ANTHROPIC_MODEL` for Claude Code is dropped, and so are
  Codex's `allow_managed_hooks_only` and `features.hooks`. Those settings would silently turn off
  routing. Dropping them means a Unity Gateway feature turns off an admin control.
