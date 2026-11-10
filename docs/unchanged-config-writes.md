# Unchanged launch configuration

Base: `222b4105` on `main`. Launch configuration calls the shared text, JSON,
TOML and dotenv writers even when the rendered value is identical. Previously
each call created/checked the parent directory and opened the file for writing,
changing its modification time. This work is unnecessary on repeated launches
and can also notify file watchers in running agents.

`write_text_file` now compares the existing text using the requested encoding
before writing. JSON and TOML delegate their rendered text to that helper;
dotenv already did. Unchanged files retain their bytes, permissions and mtime.
Text comparison preserves existing newline conventions rather than normalizing
an otherwise equivalent file. Missing/changed files still follow the same write
path. An unreadable or invalidly encoded comparison falls back to attempting the
write; write failures retain the actionable error. Dry runs still print without
reading or writing the target.

The change adds no cache and does not alter the atomic JSON writer, backup/restore
behavior, privileged managed-file installation or state-persistence protocol.
It does not make shared-file read/modify/write transactional; concurrent writers
retain the same limitations as before.

## Measurements

Linux, Python 3.12.3, 15 samples after one warmup, 100 iterations per sample.
Each iteration renders four unchanged synthetic local configs: text, JSON, TOML
and dotenv. The latter three contain 40 settings.

| Per iteration | Before | After |
| --- | ---: | ---: |
| Median elapsed time | 1.336 ms | 0.903 ms |
| File writes | 4 | 0 |

The local elapsed saving is small: approximately 0.43 ms per four-file iteration.
The more robust outcome is eliminating writes and mtime changes. Native agent
watcher behavior, network-mounted filesystems and complete startup were not
measured; no end-to-end latency claim is made.

```sh
uv run --no-sync python scripts/benchmark_config_writes.py --source /path/to/baseline/src
uv run --no-sync python scripts/benchmark_config_writes.py --source src
```

The benchmark uses temporary files, emits raw samples and counts actual
`Path.write_text` calls. It never launches an agent or accesses a workspace.

Validation: 1,863 config, agent, MCP, skill-download and integration-contract
component tests pass with an isolated home and inherited UG environment removed. All four
unchanged-write regression cases fail against the baseline and pass with the
fix. Ruff, Ruff formatting and type checking pass. Live integration was not run.
