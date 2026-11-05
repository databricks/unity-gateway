"""Draft release notes from changes.txt, grouped by change type."""

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[3]
version = sys.argv[1]
groups: dict[str, list[str]] = {}
for line in (root / "changes.txt").read_text().splitlines():
    kind, _, text = line.partition(": ")
    groups.setdefault(kind.strip(), []).append(text.strip())
print(f"# Release {version}\n")
for kind in sorted(groups):
    print(f"## {kind.title()}\n")
    print("\n".join(f"- {item}" for item in groups[kind]) + "\n")
log = root / ".skill_log" / "runs.jsonl"
log.parent.mkdir(exist_ok=True)
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"version": version}) + "\n")
