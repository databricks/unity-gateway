"""Validate the default-branch scanner's PR request without executing PR code."""

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parent.parent / ".github/workflows/security-scan.yml"
HEAD = "a" * 40
BASE = "b" * 40
RUN_ID = 123


def _resolve(tmp_path, monkeypatch, change=None, notification=None, run_error=None):
    request = {
        "id": RUN_ID,
        "event": "pull_request",
        "path": ".github/workflows/ci.yml",
        "display_title": "CI #7",
        "head_sha": HEAD,
        "head_repository": {"id": 12},
        "head_branch": "topic",
        "author_association": "OWNER",  # This untrusted hint must never set policy.
    }
    if notification is None:
        notification = dict(request)
    pr = {
        "number": 7,
        "state": "open",
        "head": {"sha": HEAD, "repo": {"id": 12, "full_name": "external/fork"}, "ref": "topic"},
        "base": {"sha": BASE, "repo": {"full_name": "databricks/unity-gateway"}},
        "author_association": "NONE",
        "user": {"login": "contributor", "type": "User"},
        "labels": [{"name": "reviewed"}],
    }
    if change:
        source, path, value = change
        target = request if source == "request" else pr
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": notification}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("REPO", "databricks/unity-gateway")

    def fetch(args, timeout):
        assert timeout == 30
        if args == ["gh", "api", f"repos/databricks/unity-gateway/actions/runs/{RUN_ID}"]:
            if run_error:
                raise run_error
            return json.dumps(request).encode()
        assert args == ["gh", "api", "repos/databricks/unity-gateway/pulls/7"]
        return json.dumps(pr).encode()

    monkeypatch.setattr(subprocess, "check_output", fetch)
    resolve = WORKFLOW.read_text().split("\n  resolve:\n", 1)[1].split("\n  scan:\n", 1)[0]
    script = textwrap.dedent(resolve.split("        run: |\n", 1)[1])
    script = script.removeprefix("python3 - <<'PY'\n").removesuffix("PY\n")
    exec(compile(script, "security-scan.yml", "exec"), {})
    return json.loads(output.read_text().removeprefix("metadata="))


def test_trusted_scan_resolves_policy_from_github(tmp_path, monkeypatch):
    metadata = _resolve(tmp_path, monkeypatch)
    assert metadata == {
        "head_sha": HEAD,
        "base_sha": BASE,
        "author_association": "NONE",
        "author": "contributor",
        "author_type": "User",
        "head_repo": "external/fork",
        "labels": ["reviewed"],
    }


@pytest.mark.parametrize(
    "notification",
    [
        {"id": RUN_ID, "event": "pull_request"},
        {
            "id": RUN_ID,
            "event": "pull_request",
            "path": ".github/workflows/ci.yml",
            "display_title": "Require a trusted main Security Scan before PR tests and /integration-test",
        },
    ],
)
def test_trusted_scan_refreshes_requested_event_metadata(tmp_path, monkeypatch, notification):
    metadata = _resolve(tmp_path, monkeypatch, notification=notification)
    assert metadata["head_sha"] == HEAD
    assert metadata["author_association"] == "NONE"


@pytest.mark.parametrize("run_id", [None, 0, -1, "123", True])
def test_trusted_scan_rejects_invalid_event_run_id(tmp_path, monkeypatch, run_id):
    with pytest.raises(SystemExit, match="run ID"):
        _resolve(tmp_path, monkeypatch, notification={"id": run_id})
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(1, "gh"), subprocess.TimeoutExpired("gh", 30)]
)
def test_trusted_scan_run_lookup_fails_closed(tmp_path, monkeypatch, error):
    with pytest.raises(type(error)):
        _resolve(tmp_path, monkeypatch, run_error=error)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "change",
    [
        ("request", ("id",), RUN_ID + 1),
        ("request", ("event",), "push"),
        ("request", ("path",), ".github/workflows/other.yml"),
        ("request", ("display_title",), "Unrecognized request"),
        ("request", ("display_title",), "CI #7 extra"),
        ("request", ("display_title",), "CI #07"),
        ("pr", ("number",), 8),
        ("pr", ("state",), "closed"),
        ("pr", ("head", "sha"), "c" * 40),
        ("pr", ("head", "repo", "id"), 99),
        ("pr", ("head", "repo"), None),
        ("pr", ("head", "ref"), "another-branch"),
        ("pr", ("base", "repo", "full_name"), "another/repository"),
        ("pr", ("base", "sha"), "invalid"),
    ],
)
def test_trusted_scan_rejects_mismatched_request(tmp_path, monkeypatch, change):
    with pytest.raises(SystemExit) as result:
        _resolve(tmp_path, monkeypatch, change)
    assert result.value.code != 0
    if change[0] == "request":
        field = "run ID" if change[1] == ("id",) else change[1][0]
        assert field in str(result.value)
    assert not (tmp_path / "output").exists()


def test_scan_orchestration_and_policy_use_the_default_branch():
    workflow = WORKFLOW.read_text()
    trigger = workflow.split("\non:", 1)[1].split("\npermissions:\n", 1)[0]
    resolve, scan = workflow.split("\n  resolve:\n", 1)[1].split("\n  scan:\n", 1)
    assert "workflow_run:" in trigger and "types: [requested]" in trigger
    assert "pull_request:" not in trigger and "workflow_call:" not in trigger
    assert "workflows: [CI]" in trigger
    assert "uses:" not in resolve and "git " not in resolve
    assert "pull-requests: read" in resolve
    assert "actions: read" in resolve
    assert "runs-on: ubuntu-latest" in scan and "needs: resolve" in scan
    assert "POLICY_SHA: ${{ github.workflow_sha }}" in scan
    assert 'git fetch -q --depth=1 origin "$POLICY_SHA"' in scan
    assert 'git -C pr checkout -q --detach "$HEAD_SHA"' in scan
    assert "bootstrap" not in workflow and "${{ secrets." not in workflow
