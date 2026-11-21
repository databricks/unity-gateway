"""Run the CLI and real SDK over HTTP without a workspace or credentials."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

PUBLISHER = Path(__file__).resolve().parents[2] / "publish.py"
BASE = "/Skills/acme/skills/custom-guide"


@pytest.fixture
def api():
    state = {
        "skill": None,
        "files": {},
        "writes": [],
        "requests": [],
        "fail": False,
        "errors": [],
        "schema_failure": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, code, body):
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header(
                "Content-Type",
                "application/octet-stream"
                if isinstance(body, bytes)
                else "application/json",
            )
            self.end_headers()
            self.wfile.write(payload)

        def handle_request(self):
            url = urlsplit(self.path)
            path = unquote(url.path)
            method = self.command
            state["requests"].append((method, path))
            if path == "/.well-known/databricks-config":
                return self.respond(404, {"error_code": "NOT_FOUND"})
            assert self.headers.get("Authorization") == "Bearer local-test-only"
            if method == "GET" and path == "/api/2.1/unity-catalog/schemas/acme.skills":
                if state["schema_failure"]:
                    return self.respond(
                        403,
                        {"error_code": "PERMISSION_DENIED", "message": "private-data"},
                    )
                return self.respond(200, {"full_name": "acme.skills"})
            if path == "/api/2.1/unity-catalog/skills/acme.skills.custom-guide":
                return self.respond(
                    200 if state["skill"] else 404,
                    state["skill"] or {"error_code": "NOT_FOUND", "message": "missing"},
                )
            if method == "POST" and path == "/api/2.1/unity-catalog/skills":
                assert parse_qs(url.query) == {
                    "parent": ["schemas/acme.skills"],
                    "skill_id": ["custom-guide"],
                }
                state["skill"] = {"name": "skills/acme.skills.custom-guide"}
            elif method == "POST" and path.endswith("/finalize"):
                assert f"{BASE}/SKILL.md" in state["files"]
                state["skill"]["finalize_time"] = "2026-01-01T00:00:00Z"
            elif method == "GET" and path.startswith("/api/2.0/fs/directories"):
                directory = path.removeprefix("/api/2.0/fs/directories").rstrip("/")
                entries = {}
                for filename in state["files"]:
                    if filename.startswith(directory + "/"):
                        relative = filename[len(directory) + 1 :]
                        child = directory + "/" + relative.split("/")[0]
                        entries[child] = {
                            "path": child,
                            "is_directory": "/" in relative,
                        }
                return self.respond(200, {"contents": list(entries.values())})
            elif path.startswith("/api/2.0/fs/files"):
                filename = path.removeprefix("/api/2.0/fs/files")
                if method == "GET":
                    return self.respond(200, state["files"][filename])
                if method == "PUT":
                    if state["fail"]:
                        return self.respond(
                            403,
                            {
                                "error_code": "PERMISSION_DENIED",
                                "message": "private-data",
                            },
                        )
                    state["files"][filename] = self.rfile.read(
                        int(self.headers["Content-Length"])
                    )
                elif method == "DELETE":
                    del state["files"][filename]
                else:
                    return self.respond(400, {"message": "unexpected method"})
            else:
                return self.respond(400, {"message": "unexpected endpoint"})
            state["writes"].append((method, path))
            return self.respond(200, state["skill"] or {})

        def checked_request(self):
            try:
                self.handle_request()
            except Exception as error:  # noqa: BLE001 -- surface fixture thread failures in teardown
                state["errors"].append(repr(error))
                self.respond(400, {"message": "fixture failure"})

        do_GET = checked_request
        do_POST = checked_request
        do_PUT = checked_request
        do_DELETE = checked_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not state["errors"], state["errors"]


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "source"
    folder = root / "skills/guide"
    (folder / "references").mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: guide\ndescription: Integration fixture.\n---\nVersion one\n"
    )
    (folder / "references/old.txt").write_text("obsolete")
    (folder / "binary.bin").write_bytes(bytes(range(256)))
    (root / "skills.yaml").write_text(
        "version: 1\nskills:\n  - path: skills/guide\n"
        "    target: acme.skills.custom-guide\n"
    )
    for arguments in [
        ["init"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-m",
            "fixture",
        ],
    ]:
        subprocess.run(
            ["git", "-C", str(root), *arguments], check=True, capture_output=True
        )
    return root


def run_cli(root, tmp_path, host, *arguments):
    # Never inherit real Databricks credentials, profiles, GitHub tokens or proxies.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(
            (
                "DATABRICKS_",
                "GITHUB_",
                "GH_",
                "HTTP_",
                "HTTPS_",
                "ALL_PROXY",
                "NO_PROXY",
            )
        )
    }
    env.update(
        DATABRICKS_HOST=host,
        DATABRICKS_TOKEN="local-test-only",
        DATABRICKS_AUTH_TYPE="pat",
        DATABRICKS_CONFIG_FILE=str(tmp_path / "absent-config"),
        NO_PROXY="127.0.0.1,localhost",
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"),
    )
    report_path = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(PUBLISHER),
            "--source-directory",
            str(root),
            "--report",
            str(report_path),
            *arguments,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert report_path.exists(), result.stderr
    return result, json.loads(report_path.read_text())


def test_publish_update_prune_and_idempotence_over_http(api, checkout, tmp_path):
    host, state = api
    result, report = run_cli(checkout, tmp_path, host)
    assert result.returncode == 0, (result.stdout, result.stderr, report)
    assert report["results"][0]["status"] == "created"
    assert len(report["source_commit"]) == 40
    assert state["files"][f"{BASE}/binary.bin"] == bytes(range(256))
    assert state["files"][f"{BASE}/references/old.txt"] == b"obsolete"
    writes = state["writes"].copy()
    result, report = run_cli(checkout, tmp_path, host)
    assert result.returncode == 0
    assert report["results"][0]["status"] == "unchanged"
    assert state["writes"] == writes

    folder = checkout / "skills/guide"
    (folder / "references/old.txt").unlink()
    (folder / "references/new.txt").write_bytes(b"new\x00reference")
    (folder / "SKILL.md").write_text(
        (folder / "SKILL.md").read_text().replace("Version one", "Version two")
    )
    result, report = run_cli(checkout, tmp_path, host)
    assert result.returncode == 1
    assert report["results"][0]["status"] == "failed"
    assert state["writes"] == writes
    result, report = run_cli(checkout, tmp_path, host, "--prune-files")
    assert result.returncode == 0
    assert report["results"][0]["uploaded"] == 2
    assert report["results"][0]["removed"] == 1
    expected = {
        f"{BASE}/{p.relative_to(folder).as_posix()}": p.read_bytes()
        for p in folder.rglob("*")
        if p.is_file()
    }
    assert state["files"] == expected
    assert "updated" in (tmp_path / "summary.md").read_text()
    writes = state["writes"].copy()
    result, report = run_cli(checkout, tmp_path, host, "--prune-files")
    assert result.returncode == 0
    assert report["results"][0]["status"] == "unchanged"
    assert state["writes"] == writes


def test_upload_failure_has_failing_exit_and_redacted_report(api, checkout, tmp_path):
    host, state = api
    state["fail"] = True
    result, report = run_cli(checkout, tmp_path, host)
    assert result.returncode == 1
    assert report["results"][0]["status"] == "failed"
    assert (
        "private-data"
        not in json.dumps(report)
        + result.stdout
        + result.stderr
        + (tmp_path / "summary.md").read_text()
    )
    assert not any(path.endswith("/finalize") for _, path in state["writes"])


@pytest.mark.parametrize("mode", ["mapping", "directory"])
def test_dry_run_makes_no_http_requests(api, checkout, tmp_path, mode):
    host, state = api
    arguments = ["--dry-run"]
    if mode == "directory":
        (checkout / "skills.yaml").unlink()
        arguments += ["--skills-directory", "skills", "--target-schema", "acme.skills"]
    result, report = run_cli(checkout, tmp_path, host, *arguments)
    assert result.returncode == 0
    assert report["results"][0]["status"] == "validated"
    assert state["requests"] == []


def test_invalid_checkout_fails_before_http(api, checkout, tmp_path):
    host, state = api
    (checkout / "skills/guide/SKILL.md").write_text("invalid")
    result, report = run_cli(checkout, tmp_path, host)
    assert result.returncode == 1
    assert "frontmatter" in report["error"]
    assert state["requests"] == []


def test_schema_failure_names_destination_without_leaking_api_body(
    api, checkout, tmp_path
):
    host, state = api
    state["schema_failure"] = True
    result, report = run_cli(checkout, tmp_path, host)
    assert result.returncode == 1
    assert "acme.skills" in report["error"]
    assert "must already exist and be accessible" in report["error"]
    assert (
        "private-data"
        not in json.dumps(report)
        + result.stdout
        + result.stderr
        + (tmp_path / "summary.md").read_text()
    )
    assert not state["writes"]


def test_lowercase_proxies_do_not_escape_local_fixture(
    api, checkout, tmp_path, monkeypatch
):
    host, _ = api
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("all_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "example.invalid")
    result, report = run_cli(checkout, tmp_path, host)
    assert result.returncode == 0, (result.stderr, report)
