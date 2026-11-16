#!/usr/bin/env python3
"""Scan a PR's *added* lines for secret-exfiltration and obfuscated-exec shapes.

Adapted from omnigent-ai/omnigent (Apache-2.0), .github/scripts/security-scan/.

Part of the single contributor Security Scan (.github/workflows/security-scan.yml),
the companion to secret-scan.py: that one flags secrets a PR *commits*, this one
flags code a PR adds to *steal* the CI secrets it runs with (the test-gateway
token, GITHUB_TOKEN) -- an env-secret read piped to the network is the shape that
matters.

It reads diff TEXT only -- it never checks out or executes the PR's code -- so it
is safe on any event. It is defense-in-depth + a reviewer aid, NOT a guarantee:
an attacker can obfuscate past regexes, so maintainer review remains the primary
gate. Its job is to (a) hard-fail on high-confidence exfiltration shapes in ADDED
lines, and (b) surface changes to files that run during CI bootstrap so the
reviewer looks harder.

Findings are two tiers:
  - BLOCKING -> non-zero exit: exfil shapes -- a secret-named credential source
    AND a network sink added to the same file; a wholesale ``os.environ`` dump; a
    decode-then-exec; or a raw TCP / reverse-shell sink.
  - INFO -> ``::warning`` only: edits to CI-bootstrap-executed files (conftest.py,
    setup.py, pyproject build hooks, anything under .github/, pytest plugins).

Env in:  DIFF_FILE (path to a ``git diff base...head`` / ``gh pr diff`` unified diff).
Exit:    non-zero if any BLOCKING finding; 0 otherwise.
"""

from __future__ import annotations

import os
import re
import sys

from diff_utils import diff_path

# Network / exfil sinks.
_NETWORK = re.compile(
    r"requests\.(get|post|put|patch|request|Session)"
    r"|urllib\.request|urlopen|httpx\.|aiohttp|http\.client"
    r"|socket\.(socket|create_connection)|telnetlib|smtplib|ftplib"
    r"|\bcurl\b|\bwget\b|\bnc\b",
    re.IGNORECASE,
)

# Browser-style sinks count only in JS/TS files: Python test helpers are often named `fetch`.
_NETWORK_JS = re.compile(r"\bfetch\(|XMLHttpRequest|axios|\bhttps?\.request\(")
_JS_FILE = re.compile(r"\.(js|mjs|cjs|jsx|ts|tsx)$")

# Secret-NAMED credential sources: the env var names UG's CI fills from secrets
# (tests/test_security_scan.py keeps the list in sync), plus runner tokens and
# credential files. Case-sensitive whole names, so code like `get_databricks_token`
# or a test's `_SESSION_SECRET` doesn't count.
_SECRET = re.compile(
    r"\b(?:CLAUDE_CODE_OAUTH_TOKEN|DATABRICKS_(?:SECOND_)?BEARER|DATABRICKS_TOKEN"
    r"|[A-Z][A-Z0-9_]*_CLIENT_SECRET|SLACK_WEBHOOK|GITHUB_TOKEN|GH_TOKEN"
    r"|ACTIONS_RUNTIME_TOKEN|ACTIONS_ID_TOKEN_REQUEST_TOKEN|PRIVATE_KEY)\b"
    r"|\.databrickscfg|/proc/self/environ"
)

# Always-blocking single-line shapes (independent of co-occurrence).
_STANDALONE = re.compile(
    r"/dev/tcp/"  # bash reverse shell
    # Wholesale environ dump only -- a bare `os.environ)` matched benign
    # `helper(os.environ)` and is dropped to avoid false positives.
    r"|(json\.dumps|dict|str|repr)\(\s*os\.environ"  # dump the whole environ
    # Not after `.`: JS `regex.exec(` is a method call, not dynamic exec.
    r"|(?<![\w.$])(?:eval|exec)\s*\(|__import__\s*\("  # dynamic exec
    r"|pickle\.loads|marshal\.loads",  # deserialization exec
    re.IGNORECASE,
)
_DECODE = re.compile(r"base64|b64decode|decodebytes|fromhex|codecs\.decode", re.IGNORECASE)
_EXEC = re.compile(
    r"(?<![\w.$])(?:eval|exec)\s*\(|__import__\s*\("
    r"|\bsubprocess\.\w+\s*\(|\bos\.(?:system|popen)\s*\(|\bpopen\s*\(",
    re.IGNORECASE,
)

# Files that execute during `uv sync` / pytest collection -- INFO, so the
# reviewer scrutinizes them even when no exfil pattern is present.
_HIGH_RISK = re.compile(
    r"(^|/)conftest\.py$|(^|/)setup\.py$|(^|/)pyproject\.toml$"
    r"|^\.github/|(^|/)sitecustomize\.py$|\.pth$"
    r"|(^|/)_token_usage\.py$|(^|/)noxfile\.py$|(^|/)tox\.ini$|(^|/)Makefile$|(^|/)[Jj]ustfile$",
)


def _changed_files_and_added(diff: str) -> dict[str, list[str]]:
    """
    Group a unified diff's ADDED lines by destination file.

    :param diff: Full unified-diff text (e.g. from ``gh pr diff``).
    :returns: Mapping of file path (e.g. ``"tests/conftest.py"``) to the list of
        added line bodies (without the leading ``+``); diff headers excluded.
    """
    by_file: dict[str, list[str]] = {}
    current: str | None = None
    in_hunk = False
    # Split on "\n" only, and read paths only from file headers, so added content (a line
    # starting with "++", a \r or \f) can't pose as diff structure and end the file early.
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            current, in_hunk = None, False
        elif not in_hunk and line.startswith("+++ "):
            current = diff_path(line[4:])
            by_file.setdefault(current, [])
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and current is not None and line.startswith("+"):
            by_file[current].append(line[1:])
    return by_file


def scan_diff(diff: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """
    Classify a unified diff into blocking and info findings.

    :param diff: Full unified-diff text.
    :returns: ``(blocking, info)`` -- two lists of ``(path, message)`` tuples.
        ``blocking`` non-empty means the scan is not clean.
    """
    by_file = _changed_files_and_added(diff)
    blocking: list[tuple[str, str]] = []
    info: list[tuple[str, str]] = []

    for path, added in by_file.items():
        body = "\n".join(added)
        has_net = bool(_NETWORK.search(body)) or bool(
            _JS_FILE.search(path) and _NETWORK_JS.search(body)
        )
        has_secret = bool(_SECRET.search(body))
        if has_net and has_secret:
            blocking.append((path, "exfil shape: secret-named source + network sink in one file"))
        for ln in added:
            if _STANDALONE.search(ln):
                blocking.append((path, f"high-risk call: {ln.strip()[:80]}"))
                break
            if _DECODE.search(ln) and _EXEC.search(ln):
                blocking.append((path, f"decode+exec: {ln.strip()[:80]}"))
                break
        if _HIGH_RISK.search(path):
            info.append((path, "touches a file that runs during CI bootstrap; review closely"))

    return blocking, info


def main() -> int:
    """
    Scan the diff at ``$DIFF_FILE`` and report exfil / obfuscated-exec findings.

    :returns: 1 if any blocking finding, else 0.
    """
    diff_path = os.environ.get("DIFF_FILE")
    if not diff_path or not os.path.isfile(diff_path):
        print(f"::error::diff file {diff_path!r} missing")
        return 1

    with open(diff_path, encoding="utf-8", errors="replace", newline="\n") as fh:
        diff = fh.read()
    blocking, info = scan_diff(diff)

    for path, msg in info:
        print(f"::warning file={path}::{msg}")
    for path, msg in blocking:
        print(f"::error file={path}::{msg}")

    if blocking:
        print(f"::error::Exfil scan found {len(blocking)} blocking finding(s) in added lines.")
        return 1
    print(f"Exfil scan passed ({len(info)} CI-file note(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
