Audit every file in `data/` with subagents. Start one subagent per file, and don't compute the checksums yourself.

Each subagent computes its file's SHA-256 and writes `audit/<file name>.txt` containing one line: `sha256: <hex digest>`.
