"""Opt-in live cloud lifecycle checks; runs installed ug, SSH and real models.

Usage: python tests/cloud_live.py --ug /path/to/ug --profile PROFILE
Requires Sandbox-capable Databricks CLI and authenticated profile. Creates billable
Sandboxes; deletes only environments created by this driver. Evidence stays local.
"""

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ug", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--claude-model", required=True)
    parser.add_argument("--codex-model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    owned = set()

    def call(name, argv, expected=0):
        started = time.monotonic()
        result = subprocess.run(argv, text=True, capture_output=True, timeout=360)
        record = {
            "name": name,
            "command": argv,
            "exit_code": result.returncode,
            "seconds": round(time.monotonic() - started, 2),
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        records.append(record)
        (args.output / f"{name}.json").write_text(json.dumps(record, indent=2))
        for sid in re.findall(r"Sandbox: ([a-z0-9-]+)", result.stderr):
            if "--sandbox" not in argv:
                owned.add(sid)
        print(f"{name}: exit {result.returncode}, {record['seconds']}s", flush=True)
        if expected is not None:
            assert result.returncode == expected, f"{name}: {result.stderr[-2000:]}"
        return result

    base = [args.ug, "cloud", "--profile", args.profile]
    native = ["databricks", "sandbox"]

    def listing():
        result = subprocess.run(
            [*native, "list", "--json", "-p", args.profile],
            text=True,
            capture_output=True,
            timeout=60,
            check=True,
        )
        return {row["sandboxId"] for row in json.loads(result.stdout)}

    prompt = "Use Python to compute sum([10,20,30]). Then reply CLOUD_OK_60."
    variants = {
        "claude": [
            "claude",
            "--disable-smart-routing",
            "--model",
            args.claude_model,
            "-p",
            prompt,
            "--allowedTools",
            "Bash",
        ],
        "codex": ["codex", "exec", "--skip-git-repo-check", "--model", args.codex_model, prompt],
    }
    try:
        call("help", [args.ug, "cloud", "--help"])
        for agent, tail in variants.items():
            result = call(f"new-{agent}", [*base, *tail])
            assert "CLOUD_OK_60" in result.stdout
            sid = re.search(r"Sandbox: ([a-z0-9-]+)", result.stderr)[1]
            assert f"Deleted Sandbox {sid}." in result.stderr
            assert sid not in listing()
            owned.discard(sid)
        created = call(
            "create-reuse",
            [*native, "create", "--name", "ug-cloud-live-test", "--json", "-p", args.profile],
        )
        sid = json.loads(created.stdout)["sandboxId"]
        owned.add(sid)
        for agent, tail in variants.items():
            result = call(f"reuse-{agent}", [*base, "--sandbox", sid, *tail])
            assert "CLOUD_OK_60" in result.stdout
            assert sid in listing()
        failure = call(
            "task-failure-cleanup",
            [*base, "claude", "--model", "nonexistent-cloud-test-model", "-p", "Reply OK"],
            expected=None,
        )
        assert failure.returncode != 0
        failed_sid = re.search(r"Sandbox: ([a-z0-9-]+)", failure.stderr)[1]
        assert f"Deleted Sandbox {failed_sid}." in failure.stderr
        assert failed_sid not in listing()
        owned.discard(failed_sid)
        call("delete-reuse", [*base, "delete", sid, "--yes"])
        assert sid not in listing()
        owned.discard(sid)
    finally:
        for sid in owned:
            call(f"cleanup-{sid}", [*native, "delete", sid, "--auto-approve", "-p", args.profile])
        (args.output / "summary.json").write_text(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
