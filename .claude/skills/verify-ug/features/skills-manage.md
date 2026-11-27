---
feature: skills-manage
title: ug skills (list, remove, add)
platform: cli
paths: [src/ucode/cli.py, src/ucode/skills_list.py, src/ucode/skills_download.py, src/ucode/skills_state.py]
status: 'unverified: list, download-side remove, add (empty schema) and add --via mcp driven on staging; real skill download and configured bare skills not driven'
---

# ug skills

List and remove Unity Catalog skills ug downloaded or scoped into the skills MCP. Group `skill_app` `cli.py:2097`; bare callback `cli.py:2574`, `list` `cli.py:2600`, `add` `cli.py:2614`, `remove` `cli.py:2724`.

## Reach

From a shell, run `ug skills <subcommand>` with the isolated environment in `../SKILL.md` (Environment).

## Drive

Env: `HOME=$H UG_SKIP_OS_SETTINGS=1`, stdin `</dev/null`, and a working `databricks` on PATH (see SKILL.md Environment). Seed the manifest `$H/.ucode/skills.json` (`skills_state.py:1-9,40-70`):
`{"version":1,"skill_downloads":[{"fqn":"main.demo.hello","bundle_name":"hello","workspace":"https://example.cloud.databricks.com","scope":"personal","base":"$H/proj","dirs":["$H/proj/.claude/skills/hello","$H/proj/.agents/skills/hello"]}]}`
and create those dirs with a SKILL.md each, plus an unattributed `$H/proj/.claude/skills/mine`.

1. [verified] `ug skills list` (empty, then seeded).
2. [verified] `ug skills remove --location main.other --path $H/proj`; `ug skills remove --names main.demo.hello --path /tmp/elsewhere`.
3. [verified] `ug skills remove`; `ug skills remove --names bad`; `ug skills remove --via mcp`.
4. [verified] `ug skills remove --names main.demo.hello --path $H/proj`, then `ug skills list`.
5. [verified, staging workspace] With the saved workspace from `ug mcp add` (see mcp-manage) and a cached login: `ug skills add --location system.ai --path $H/skills`.
6. [verified, staging workspace] `ug skills add --location system.ai --via mcp --agents cursor`, then `ug skills list`.
7. [unverified: no skills in `system.ai` on staging] Real download: `ug skills add --location <c.s> [--names c.s.n] [--path DIR] [--agents ...]` against a schema that has skills. `--location` is required when stdin is not a TTY.
8. [verified, empty HOME only] Bare `ug skills`. With a saved workspace it registers the schema-less skills MCP (`cli.py:2574`); not driven.

## Expect

- 1: exit 0. Empty: "No skills configured. Use `ug skills add`...". Seeded: table `NAME LOCATION VIA AGENTS` with `hello main.demo local all`.
- 2: exit 0, notes "No developer-downloaded skills from `main.other` under ..." / "...matching `main.demo.hello` under `/tmp/elsewhere`."; nothing deleted.
- 3: exit 1: "--location or --names is required for `ug skills remove`."; "--names entries must be fully-qualified ... (invalid: bad)."; "--location is required for `ug skills remove --via mcp`."
- 4: exit 0, "Removed 1 downloaded skill(s)." Both attributed hello dirs are deleted, `mine/SKILL.md` is kept, `skills.json` becomes `{"version":1,"skill_downloads":[]}`, list says no skills configured.

- 5: exit 0, "No skills found in `system.ai`."; no files written.
- 6: exit 0, "Skills MCP registered", server `databricks-skill-registry`, URL `<ws>/ai-gateway/skills/?schema=system.ai`, "Configured: Cursor"; `.cursor/mcp.json` gains that server. `ug skills list` still says "No skills configured.", since MCP-route skills aren't listed.
- 8: exit 1 in an empty HOME. It prints help, then "Workspace is not configured".

## Gotchas

- `skills add` has no `--workspace` option (exit 2, "No such option"); it uses the saved workspace.

- Every skills subcommand starts with `install_databricks_cli(minimum=...)`. Under a fresh HOME a `databricks` wrapper that depends on the real HOME fails with "Could not parse Databricks CLI version ...", exit 1, before any logic. Put a working CLI first on PATH (SKILL.md Environment). With a readable but too-old CLI it would try to upgrade (network); not hit here.
- list and remove also write `.databricks/token-cache.json` and `.cache/databricks/...` under HOME from the CLI probe; `state.json` is also rewritten if present.
- Picker flows (no flags on a TTY) not driven.
