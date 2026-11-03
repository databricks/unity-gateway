"""Publish skill bundles with Databricks unified authentication, from any Git checkout."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import AlreadyExists, NotFound
from databricks.sdk.service.catalog import Skill

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,254}$")
BUNDLE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


@dataclass(frozen=True)
class Bundle:
    path: str
    target: str
    files: dict[str, bytes]
    sha256: str


def confined(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f"Expected a nonempty relative path: {relative!r}")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes source directory: {relative}")
    # Reject symlink ancestors as well as symlink files; publishing must not depend
    # on files outside the checkout or follow links unintentionally.
    if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValueError(f"Symlink paths are unsupported: {relative}")
    return path


def validate_target(target: str, parts: int = 3) -> None:
    if not isinstance(target, str) or len(target.split(".")) != parts:
        raise ValueError(
            f"Expected {'catalog.schema.skill' if parts == 3 else 'catalog.schema'}: {target!r}"
        )
    if not all(NAME.fullmatch(p) for p in target.split(".")):
        raise ValueError(
            f"Unsupported UC identifier in {target!r}; use lowercase letters, numbers, underscores or hyphens"
        )


def read_bundle(root: Path, path: str, target: str) -> Bundle:
    validate_target(target)
    folder = confined(root, path)
    if not folder.is_dir():
        raise ValueError(f"Skill folder does not exist: {path}")
    files = {}
    for file in sorted(folder.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"Symlinks are unsupported: {file.relative_to(root)}")
        if file.is_file():
            relative = file.relative_to(folder).as_posix()
            if ".git" in file.relative_to(folder).parts:
                raise ValueError(f"Nested Git metadata in skill folder: {path}")
            files[relative] = file.read_bytes()
    text = files.get("SKILL.md", b"").decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"Missing YAML frontmatter in {path}/SKILL.md")
    metadata = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not BUNDLE_NAME.fullmatch(metadata["name"])
    ):
        raise ValueError(f"Invalid skill frontmatter name in {path}")
    description = metadata.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description.encode("utf-8")) > 1024
    ):
        raise ValueError(f"Description must contain 1-1024 UTF-8 bytes in {path}")
    digest = hashlib.sha256()
    for relative, content in files.items():
        digest.update(
            relative.encode() + b"\0" + str(len(content)).encode() + b"\0" + content
        )
    return Bundle(path, target, files, digest.hexdigest())


def load_bundles(
    root: Path,
    mapping_file: str | None = None,
    skills_directory: str | None = None,
    target_schema: str | None = None,
) -> list[Bundle]:
    if skills_directory or target_schema:
        if mapping_file or not (skills_directory and target_schema):
            raise ValueError(
                "Use skills-directory and target-schema together, or a mapping-file"
            )
        config = {
            "version": 1,
            "directories": [{"path": skills_directory, "schema": target_schema}],
        }
    else:
        config = yaml.safe_load(
            confined(root, mapping_file or "skills.yaml").read_text()
        )
    if (
        not isinstance(config, dict)
        or set(config) - {"version", "skills", "directories"}
        or config.get("version") != 1
    ):
        raise ValueError(
            "Mapping requires version: 1; supported keys: version, skills, directories"
        )
    mappings = []
    for entry in config.get("skills", []):
        if not isinstance(entry, dict) or set(entry) != {"path", "target"}:
            raise ValueError("Each skills entry requires only path and target")
        mappings.append((entry["path"], entry["target"]))
    for entry in config.get("directories", []):
        if not isinstance(entry, dict) or set(entry) != {"path", "schema"}:
            raise ValueError("Each directories entry requires only path and schema")
        validate_target(entry["schema"], parts=2)
        directory = confined(root, entry["path"])
        if not directory.is_dir():
            raise ValueError(f"Directory does not exist: {entry['path']}")
        for child in sorted(directory.iterdir()):
            if child.is_symlink():
                raise ValueError(f"Symlink in discovery directory: {child}")
            if child.is_dir() and (child / "SKILL.md").exists():
                mappings.append(
                    (
                        child.relative_to(root).as_posix(),
                        f"{entry['schema']}.{child.name}",
                    )
                )
    if not mappings:
        raise ValueError("Mapping resolved to zero skills")
    targets = [target for _, target in mappings]
    if len(set(targets)) != len(targets):
        raise ValueError(
            "Multiple folders map to the same UC skill; use distinct targets"
        )
    # Validate and snapshot every local bundle before any remote writes.
    return [read_bundle(root, path, target) for path, target in mappings]


def remote_files(w: WorkspaceClient, base: str) -> set[str]:
    pending = [base]
    files = set()
    while pending:
        directory = pending.pop()
        for entry in w.files.list_directory_contents(directory):
            if not entry.path:
                raise ValueError("Files API returned an entry without a path")
            path = PurePosixPath(entry.path)
            relative = path.relative_to(PurePosixPath(base)).as_posix()
            if ".." in path.parts or relative == ".":
                raise ValueError("Files API returned an invalid bundle path")
            if entry.is_directory:
                pending.append(entry.path)
            else:
                files.add(relative)
    return files


def publish_bundle(w: WorkspaceClient, bundle: Bundle, prune_files: bool) -> dict:
    result = {
        "path": bundle.path,
        "target": bundle.target,
        "sha256": bundle.sha256,
        "files": len(bundle.files),
    }
    catalog, schema, leaf = bundle.target.split(".")
    base = f"/Skills/{catalog}/{schema}/{leaf}"
    name = f"skills/{bundle.target}"
    try:
        try:
            existing = w.ai_gateway.get_skill(name=name)
        except NotFound:
            existing = None
        current = remote_files(w, base) if existing else set()
        stale = current - bundle.files.keys()
        if stale and not prune_files:
            raise ValueError(
                f"Remote bundle has {len(stale)} stale file(s). Set prune-files: true to mirror this mapped folder"
            )
        changed = []
        for relative, content in bundle.files.items():
            if relative in current:
                response = w.files.download(f"{base}/{relative}")
                with response.contents as stream:
                    if stream.read() == content:
                        continue
            changed.append(relative)
        if existing and existing.finalize_time and not changed and not stale:
            return {**result, "status": "unchanged", "uploaded": 0, "removed": 0}
        if not existing:
            try:
                w.ai_gateway.create_skill(
                    parent=f"schemas/{catalog}.{schema}", skill_id=leaf, skill=Skill()
                )
            except AlreadyExists:
                # Another writer raced this run; do not overwrite its contents.
                raise ValueError(
                    "Skill appeared during publishing; serialize writers and retry"
                ) from None
        # Upload SKILL.md last to reduce the window of mixed metadata/content.
        for relative in sorted(changed, key=lambda p: (p == "SKILL.md", p)):
            w.files.upload(
                f"{base}/{relative}", io.BytesIO(bundle.files[relative]), overwrite=True
            )
        for relative in sorted(stale):
            w.files.delete(f"{base}/{relative}")
        w.ai_gateway.finalize_skill(name=name)
        return {
            **result,
            "status": "updated" if existing else "created",
            "uploaded": len(changed),
            "removed": len(stale),
        }
    except Exception as error:  # noqa: BLE001 -- contain per-skill failures and emit a report
        # Don't include API exception bodies: upstream errors can contain sensitive
        # request details. The exception type and target make failures diagnosable.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        return {**result, "status": "failed", "error": message}


def source_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", default=".")
    parser.add_argument("--mapping-file")
    parser.add_argument("--skills-directory")
    parser.add_argument("--target-schema")
    parser.add_argument("--profile", help="Databricks CLI/SDK profile for local use")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prune-files", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--report", default="uc-skills-report.json")
    args = parser.parse_args()
    root = Path(args.source_directory).resolve()
    report = {
        "source_commit": source_commit(root),
        "dry_run": args.dry_run,
        "results": [],
    }
    try:
        bundles = load_bundles(
            root, args.mapping_file, args.skills_directory, args.target_schema
        )
        if args.dry_run:
            report["results"] = [
                {
                    "path": b.path,
                    "target": b.target,
                    "status": "validated",
                    "sha256": b.sha256,
                    "files": len(b.files),
                }
                for b in bundles
            ]
        else:
            w = (
                WorkspaceClient(profile=args.profile)
                if args.profile
                else WorkspaceClient()
            )
            # Existing schemas are deliberate: don't silently create governance boundaries.
            for schema in sorted({".".join(b.target.split(".")[:2]) for b in bundles}):
                w.schemas.get(schema)
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                report["results"] = list(
                    pool.map(lambda b: publish_bundle(w, b, args.prune_files), bundles)
                )
    except Exception as error:  # noqa: BLE001 -- contain per-skill failures and emit a report
        report["error"] = (
            str(error)
            if isinstance(error, (ValueError, OSError, yaml.YAMLError))
            else type(error).__name__
        )
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    rows = ["| Skill | Status | Files |", "|---|---|---|"]
    for result in report["results"]:
        print(
            f"{result['status']}: {result['target']}"
            + (f" ({result['error']})" if "error" in result else "")
        )
        rows.append(
            f"| `{result['target']}` | {result['status']} | {result['files']} |"
        )
    if "error" in report:
        print(f"Publishing failed: {report['error']}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as output:
            output.write(
                f"## UC skill publishing\n\nSource commit: `{report['source_commit']}`\n\n"
                + "\n".join(rows)
                + "\n"
            )
            if "error" in report:
                output.write("\nValidation or setup failed. See the JSON report.\n")
    return int(
        "error" in report or any(r["status"] == "failed" for r in report["results"])
    )


if __name__ == "__main__":
    raise SystemExit(main())
