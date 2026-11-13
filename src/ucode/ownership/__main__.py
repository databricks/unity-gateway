"""Regenerate or check the ownership-table section of the admin docs.

    uv run python -m ucode.ownership --write docs/admin-managed-settings-reference.md
    uv run python -m ucode.ownership --check docs/admin-managed-settings-reference.md

``--check`` exits 1 when the docs are out of date with the tables. ``tests/test_ownership.py``
runs the same check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ucode.ownership import replace_docs_section


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ucode.ownership")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", metavar="DOCS", type=Path, help="Rewrite the generated section.")
    mode.add_argument("--check", metavar="DOCS", type=Path, help="Fail if the section is stale.")
    args = parser.parse_args(argv)
    path: Path = args.write or args.check
    current = path.read_text(encoding="utf-8")
    try:
        expected = replace_docs_section(current)
    except ValueError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    if args.write:
        if expected != current:
            path.write_text(expected, encoding="utf-8")
        return 0
    if expected != current:
        print(
            f"{path} is out of date with src/ucode/ownership/*.toml. Run:\n"
            f"  uv run python -m ucode.ownership --write {path}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
