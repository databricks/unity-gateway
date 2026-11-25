"""Validate the default-branch scanner's PR request without executing PR code."""

import copy
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parent.parent / ".github/workflows/security-scan.yml"
HEAD = "a" * 40
BASE = "b" * 40
RUN_ID = 123


def test_scan_concurrency_isolates_each_ci_request():
    workflow = WORKFLOW.read_text()
    concurrency = workflow.split("\nconcurrency:\n", 1)[1].split("\njobs:\n", 1)[0]
    assert "group: security-scan-${{ github.event.workflow_run.id }}" in concurrency
    assert "cancel-in-progress: true" in concurrency


def _resolve(
    tmp_path,
    monkeypatch,
    change=None,
    detail_change=None,
    notification=None,
    run_error=None,
    candidates=None,
    branch="topic",
):
    request = {
        "id": RUN_ID,
        "event": "pull_request",
        "path": ".github/workflows/ci.yml",
        "display_title": "CI #7",
        "head_sha": HEAD,
        "head_repository": {"id": 12, "owner": {"login": "external"}},
        "head_branch": branch,
        "author_association": "OWNER",  # This untrusted hint must never set policy.
    }
    if notification is None:
        notification = dict(request)
    pr = {
        "number": 7,
        "state": "open",
        "head": {"sha": HEAD, "repo": {"id": 12, "full_name": "external/fork"}, "ref": branch},
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
    # The PR-detail re-fetch sees a possibly different payload than the head listing.
    detail = copy.deepcopy(pr)
    if detail_change:
        _, path, value = detail_change
        target = detail
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": notification}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("REPO", "databricks/unity-gateway")

    queries = []

    def fetch(args, timeout):
        assert timeout == 30
        if args == ["gh", "api", f"repos/databricks/unity-gateway/actions/runs/{RUN_ID}"]:
            if run_error:
                raise run_error
            return json.dumps(request).encode()
        if args[2].startswith("repos/databricks/unity-gateway/pulls?"):
            queries.append(args[2])
            return json.dumps([pr] if candidates is None else candidates).encode()
        assert args == ["gh", "api", f"repos/databricks/unity-gateway/pulls/{pr['number']}"]
        return json.dumps(detail).encode()

    monkeypatch.setattr(subprocess, "check_output", fetch)
    resolve = WORKFLOW.read_text().split("\n  resolve:\n", 1)[1].split("\n  scan:\n", 1)[0]
    script = textwrap.dedent(resolve.split("        run: |\n", 1)[1])
    script = script.removeprefix("python3 - <<'PY'\n").removesuffix("PY\n")
    exec(compile(script, "security-scan.yml", "exec"), {})
    head = "external%3A" + request["head_branch"].replace("/", "%2F")
    assert queries == [f"repos/databricks/unity-gateway/pulls?head={head}&state=open&per_page=100"]
    return json.loads(output.read_text().removeprefix("metadata="))


@pytest.mark.parametrize(
    "notification",
    [
        None,
        {"id": RUN_ID, "event": "pull_request"},
        {
            "id": RUN_ID,
            "event": "pull_request",
            "path": ".github/workflows/ci.yml",
            "display_title": "Require a trusted main Security Scan before PR tests and /integration-test",
        },
    ],
)
def test_trusted_scan_resolves_policy_from_github(tmp_path, monkeypatch, notification):
    metadata = _resolve(tmp_path, monkeypatch, notification=notification)
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
    "display_title", ["PR title", "CI #7 extra", "CI #07", "CI #8", "CI #8 (backport)"]
)
def test_trusted_scan_resolves_pr_by_head_not_run_name(tmp_path, monkeypatch, display_title):
    # A fork's CI run keeps the PR title as its name until a maintainer approves it, and a
    # title that names another PR must not fail or redirect a head-matched resolution.
    change = ("request", ("display_title",), display_title)
    assert _resolve(tmp_path, monkeypatch, change)["head_sha"] == HEAD


def test_trusted_scan_encodes_branch_names_with_slashes(tmp_path, monkeypatch):
    assert _resolve(tmp_path, monkeypatch, branch="user/topic")["head_sha"] == HEAD


@pytest.mark.parametrize(
    "change",
    [
        ("request", ("head_repository",), None),
        ("request", ("head_repository", "owner"), None),
        ("request", ("head_branch",), None),
    ],
)
def test_trusted_scan_rejects_a_run_without_a_resolvable_head(tmp_path, monkeypatch, change):
    with pytest.raises(SystemExit, match="missing its head repository or branch"):
        _resolve(tmp_path, monkeypatch, change)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "detail_change, message",
    [
        (("pr", ("number",), 8), "no longer matches"),
        (("pr", ("state",), "closed"), "no longer matches"),
        (("pr", ("head", "sha"), "c" * 40), "no longer matches"),
        (("pr", ("head", "repo", "id"), 99), "no longer matches"),
        (("pr", ("head", "repo"), None), "no longer matches"),
        (("pr", ("head", "ref"), "another-branch"), "no longer matches"),
        (("pr", ("base", "repo", "full_name"), "another/repository"), "no longer matches"),
        (("pr", ("base", "sha"), "invalid"), "valid PR commit SHAs"),
    ],
)
def test_trusted_scan_revalidates_the_fetched_pr_detail(
    tmp_path, monkeypatch, detail_change, message
):
    with pytest.raises(SystemExit, match=message):
        _resolve(tmp_path, monkeypatch, detail_change=detail_change)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("matches", [0, 2])
def test_trusted_scan_requires_exactly_one_pr_for_the_head(tmp_path, monkeypatch, matches):
    pr = {"number": 7, "head": {"sha": HEAD, "repo": {"id": 12}}}
    with pytest.raises(SystemExit, match=f"exactly one open PR head, found {matches}"):
        _resolve(tmp_path, monkeypatch, candidates=[dict(pr, number=n) for n in range(matches)])
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "stale",
    [
        {"sha": "c" * 40, "repo": {"id": 12}},
        {"sha": HEAD, "repo": {"id": 99}},
        {"sha": HEAD, "repo": None},
    ],
)
def test_trusted_scan_ignores_listed_prs_with_another_head(tmp_path, monkeypatch, stale):
    with pytest.raises(SystemExit, match="found 0"):
        _resolve(tmp_path, monkeypatch, candidates=[{"number": 7, "head": stale}])


def test_trusted_scan_picks_the_matching_pr_among_listed_heads(tmp_path, monkeypatch):
    stale = {"number": 5, "head": {"sha": "c" * 40, "repo": {"id": 12}}}
    live = {"number": 7, "head": {"sha": HEAD, "repo": {"id": 12}}}
    assert _resolve(tmp_path, monkeypatch, candidates=[stale, live])["head_sha"] == HEAD


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
        field = "run ID" if change[1][0] == "id" else change[1][0]
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
