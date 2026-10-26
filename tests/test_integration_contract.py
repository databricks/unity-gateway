"""Keep the black-box suite independent of application internals and test doubles."""

import ast
import io
import json
import os
import re
import runpy
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.integration.utils.harness import clean_environment
from tests.integration.utils.managed import assert_no_managed_config


def _markers(nodes):
    return {
        node.attr
        for root in nodes
        for node in ast.walk(root)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "pytest"
        and node.value.attr == "mark"
    }


def test_isolated_sessions_disable_antigravity_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", "1")

    assert clean_environment(tmp_path)["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


def test_integration_ci_pins_a_skills_capable_databricks_cli():
    from ucode.databricks import SKILLS_MCP_MIN_DATABRICKS_CLI_VERSION

    workflow = Path(__file__).parent.parent / ".github/workflows/integration.yml"
    setup_blocks = re.findall(
        r"(?m)^      - uses: databricks/setup-cli@[^\n]+\n((?:        [^\n]*\n)*)",
        workflow.read_text(),
    )
    assert setup_blocks, "Integration CI must install the Databricks CLI explicitly"
    for block in setup_blocks:
        version = re.search(r"(?m)^          version: (\d+)\.(\d+)\.(\d+)\s*$", block)
        assert version, "Every integration setup-cli step must pin an exact CLI version"
        assert tuple(map(int, version.groups())) >= SKILLS_MCP_MIN_DATABRICKS_CLI_VERSION


def test_managed_integration_ci_is_blocking():
    workflow = Path(__file__).parent.parent / ".github/workflows/integration.yml"
    managed, gate = workflow.read_text().split("\n  managed:\n", 1)[1].split("\n  cujs:\n", 1)
    assert "continue-on-error:" not in managed
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", gate)
    assert needs is not None
    assert "managed" in {job.strip() for job in needs.group(1).split(",")}
    assert (
        "if: ${{ always() && (github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository) }}"
    ) in gate


WORKFLOW = Path(__file__).parent.parent / ".github/workflows/integration.yml"
# The quarantined Claude Smart Router toggle journey, split into one test per toggle state.
CLAUDE_TOGGLE_TESTS = (
    "test_smart_router_routes_claude_subagent_at_launch",
    "test_smart_router_skill_turns_claude_subagent_routing_off",
    "test_smart_router_skill_turns_claude_subagent_routing_back_on",
)


def _workflow_job(name, next_name):
    workflow = WORKFLOW.read_text()
    return workflow.split(f"\n  {name}:\n", 1)[1].split(f"\n  {next_name}:\n", 1)[0]


def test_soft_fail_ci_is_non_blocking_on_both_platforms():
    jobs = _integration_jobs()
    job = jobs["soft-fail"]
    assert "continue-on-error: true" in job
    assert "os: [ubuntu-22.04, windows-latest]" in job
    assert "runs-on: ${{ matrix.os }}" in job
    assert "needs: [workspace, full, managed]" in job
    assert "!cancelled() && needs.workspace.result == 'success'" in job
    assert "fail-fast: false" in job
    assert "max-parallel: 1" in job
    assert "agent: [claude]" in job
    assert "AGENT: ${{ matrix.agent }}" in job
    assert "ARTIFACT_NAME: integration-soft-fail-${{ matrix.agent }}-${{ matrix.os }}" in job
    assert "steps: *live-steps" in job
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", jobs["cujs"])
    assert needs is not None
    assert "soft-fail" not in {name.strip() for name in needs.group(1).split(",")}
    for name in ("smoke", "full", "managed"):
        assert "continue-on-error:" not in jobs[name]
        assert "and not soft_fail" in jobs[name]


def test_soft_fail_marks_only_the_affected_claude_journey():
    marked = []
    for path in (Path(__file__).parent / "integration").glob("test_*.py"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and "soft_fail" in _markers(node.decorator_list):
                marked.append((path.name, node.name))
    assert marked == [("test_ug_smart_routing_hooks.py", name) for name in CLAUDE_TOGGLE_TESTS]


def _collect_integration_cases(expression):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--confcutdir=tests/integration",
            "tests/integration",
            "--collect-only",
            "-q",
            "-m",
            expression,
        ],
        cwd=WORKFLOW.parent.parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cases = {line for line in result.stdout.splitlines() if line.startswith("test_")}
    assert cases, result.stdout
    return cases


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_smoke_and_full_together_cover_every_blocking_live_case(agent):
    jobs = _integration_jobs()
    selected = []
    for name in ("smoke", "full"):
        marker = re.search(r"(?m)^      TEST_MARKER: (.+)$", jobs[name])
        assert marker is not None
        selected.append(
            _collect_integration_cases(marker.group(1).replace("${{ matrix.agent }}", agent))
        )
    smoke, full = selected
    # Full skips smoke cases only because smoke runs them on the same OS matrix.
    assert "os: [ubuntu-22.04, windows-latest]" in jobs["smoke"]
    assert "os: [ubuntu-22.04, windows-latest]" in jobs["full"]
    assert not smoke & full
    assert smoke | full == _collect_integration_cases(f"live and {agent} and not soft_fail")


def test_smoke_and_full_journeys_run_every_agent_and_os_in_parallel():
    jobs = _integration_jobs()
    assert "concurrency:" not in jobs["smoke"]
    assert "concurrency:" not in jobs["full"]
    assert "max-parallel: 4" in jobs["full"]


def test_full_claude_lane_includes_both_desktop_journeys():
    full = _integration_jobs()["full"]
    marker = re.search(r"(?m)^      TEST_MARKER: (.+)$", full)
    assert marker is not None
    cases = _collect_integration_cases(marker.group(1).replace("${{ matrix.agent }}", "claude"))
    assert {
        "test_ug_configure_claude_desktop.py::test_ug_configure_claude_desktop_profile_lifecycle",
        "test_ug_configure_claude_desktop.py::test_ug_configure_claude_desktop_subscription_relay",
    } <= cases
    assert "os: [ubuntu-22.04, windows-latest]" in full
    assert "CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in full


@pytest.mark.parametrize(
    ("job_name", "agent", "retained_test"),
    [
        ("smoke", "claude", "test_ug_claude_headless_prompt_argument"),
        ("smoke", "codex", "test_ug_codex_headless_prompt_argument"),
        ("full", "claude", "test_smart_routing_claude_route_subagent_hook"),
        ("full", "codex", "test_smart_router_skill_turns_codex_subagent_routing_back_on"),
        ("managed", "claude", "test_ug_configure_managed_claude"),
        ("managed", "codex", "test_smart_router_skill_turns_codex_subagent_routing_back_on"),
        ("soft-fail", "claude", None),
    ],
)
def test_soft_fail_ci_selectors_preserve_coverage(job_name, agent, retained_test):
    job = _integration_jobs()[job_name]
    if job_name == "managed":
        marker = re.search(r'-- -m "([^"]+)"', job)
    else:
        marker = re.search(r"(?m)^      TEST_MARKER: (.+)$", job)
    assert marker is not None
    expression = marker.group(1).replace("${{ matrix.agent }}", agent).replace("$AGENT", agent)
    cases = _collect_integration_cases(expression)
    if retained_test is not None:
        assert any(f"::{retained_test}" in case for case in cases), cases
        assert not any(f"::{toggle}[" in case for case in cases for toggle in CLAUDE_TOGGLE_TESTS)
        return
    assert cases == {
        f"test_ug_smart_routing_hooks.py::{toggle}[{variant}]"
        for toggle in CLAUDE_TOGGLE_TESTS
        for variant in (
            "routing-only",
            "orchestration",
            "managed-default",
            "subagent_only_v0",
            "subagent_only_v1",
            "subagent_orch_v0",
        )
    }


def _discover_cujs(root, output):
    plan = _workflow_job("dedicated-cuj-plan", "dedicated-cuj")
    script = textwrap.dedent(plan.split("        run: |\n", 1)[1])
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=root,
        env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        return None, result.stderr
    return json.loads(output.read_text().removeprefix("cujs=")), result.stderr


def test_dedicated_cuj_ci_runs_each_discovered_file_on_its_own_runner():
    job = _workflow_job("dedicated-cuj", "cujs")

    assert "docs.google.com/document/d/1WKd1fdWD0Y4tAV1H9Si-SGx2UZFL7iZ2S3HtBjidmS0" in job
    assert "name: E2E CUJs · ${{ matrix.name }}" in job
    assert "fail-fast: false" in job
    assert "include: ${{ fromJSON(needs.dedicated-cuj-plan.outputs.cujs) }}" in job
    assert 'pytest --confcutdir=tests/e2e_cuj "tests/e2e_cuj/${{ matrix.test }}"' in job
    assert 'UV_HTTP_TIMEOUT: "120"' in job and 'UV_HTTP_RETRIES: "6"' in job
    assert "UG_CUJ1_WORKSPACE: ${{ secrets.UG_CUJ1_WORKSPACE }}" in job
    assert "test_cuj" not in job


def test_latest_harness_is_scheduled_and_opt_in():
    workflow = _workflow("harness-latest.yml")
    trigger = workflow.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
    assert "cron: '0 22 * * 1-5'" in trigger
    assert "workflow_dispatch:" in trigger
    assert "pull_request" not in trigger and "push:" not in trigger
    assert "harness-latest" not in _integration_jobs()

    for package in ("@anthropic-ai/claude-code", "@openai/codex"):
        assert f"npm view {package}@latest version" in workflow
    assert "-m harness_latest" in workflow
    assert "harness_latest and managed_fixture" in workflow
    assert "harness_latest and not managed_fixture" in workflow
    assert '-m "$TEST_MARKER"' in workflow
    assert "-k " not in workflow
    assert "continue-on-error:" not in workflow


SLACK_NOTIFY = Path(__file__).parent.parent / ".github/scripts/slack-notify/notify.py"


def _assert_slack_notify_wiring(notify):
    assert "actions: read" in notify and "contents: read" in notify
    assert "sparse-checkout: .github/scripts/slack-notify/notify.py" in notify
    assert "secrets.SLACK_WEBHOOK_UG_CODING_AGENTS_POD" in notify
    assert "run: python3 .github/scripts/slack-notify/notify.py" in notify


def test_terminal_bench_runs_twice_daily_and_notifies_slack():
    workflow = _workflow("terminal-bench.yml")
    assert "cron: '0 7,19 * * *'" in workflow
    notify = workflow.split("\n  notify:\n", 1)[1]
    assert "needs: [harbor, native]" in notify
    assert "if: ${{ !cancelled() }}" in notify
    assert "NOTIFY_NAME: Terminal Bench" in notify
    assert (
        "NOTIFY_RESULT: ${{ contains(needs.*.result, 'failure') && 'failure' || 'success' }}"
        in notify
    )
    _assert_slack_notify_wiring(notify)


@pytest.mark.parametrize(
    ("result", "claude_version", "job_conclusion", "api_error", "slack_error"),
    [
        ("success", "2.1.290", "success", False, False),
        ("failure", "2.1.290", "failure", False, False),
        ("failure", "2.1.290", "timed_out", False, False),
        ("skipped", "", "failure", False, False),
        ("failure", "2.1.290", "failure", True, False),
        ("success", "2.1.290", "success", False, True),
    ],
)
def test_latest_harness_slack_notification(
    monkeypatch, capsys, result, claude_version, job_conclusion, api_error, slack_error
):
    workflow = _workflow("harness-latest.yml")
    resolver, harness = workflow.split("\n  harness-latest:\n", 1)
    harness, notify = harness.split("\n  notify:\n", 1)
    assert "outputs:" in resolver
    assert "needs: resolve-versions" in harness
    for agent in ("claude", "codex"):
        assert f"needs.resolve-versions.outputs.{agent}" in harness
    assert "needs: [resolve-versions, harness-latest]" in notify
    assert "always()" in notify
    assert "needs.harness-latest.result == 'success'" in notify
    assert "needs.harness-latest.result == 'failure'" in notify
    assert "needs.resolve-versions.result == 'failure'" in notify
    assert "NOTIFY_NAME: Latest harness test" in notify
    assert "NOTIFY_RESULT: ${{ needs.harness-latest.result }}" in notify
    _assert_slack_notify_wiring(notify)

    env = {
        "NOTIFY_NAME": "Latest harness test",
        "NOTIFY_RESULT": result,
        "CLAUDE_VERSION": claude_version,
        "CODEX_VERSION": "0.154.0",
        "GITHUB_REPOSITORY": "databricks/unity-gateway",
        "GITHUB_RUN_ID": "123",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_SERVER_URL": "https://github.com",
        "GH_TOKEN": "test-token",
        "SLACK_WEBHOOK": "https://example.invalid/slack",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    job_url = "https://github.com/databricks/unity-gateway/actions/runs/123/job/456"
    posted = []

    def urlopen(request, timeout):
        assert timeout == 30
        if request.full_url == env["SLACK_WEBHOOK"]:
            assert request.method == "POST"
            assert request.get_header("Content-type") == "application/json"
            if slack_error:
                raise urllib.error.URLError("Slack unavailable")
            posted.append(json.loads(request.data))
            return io.BytesIO(b"ok")
        assert request.full_url == (
            "https://api.github.com/repos/databricks/unity-gateway/actions/runs/123/jobs"
            "?filter=latest&per_page=100"
        )
        assert request.get_header("Authorization") == "Bearer test-token"
        if api_error:
            raise urllib.error.URLError("GitHub unavailable")
        jobs = [
            {"conclusion": "success", "html_url": "other-job"},
            {"conclusion": job_conclusion, "html_url": job_url},
        ]
        return io.BytesIO(json.dumps({"jobs": jobs}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    if slack_error:
        with pytest.raises(urllib.error.URLError, match="Slack unavailable"):
            runpy.run_path(str(SLACK_NOTIFY), run_name="__main__")
        assert not posted
        return
    runpy.run_path(str(SLACK_NOTIFY), run_name="__main__")
    versions = f"Versions used: Claude Code {claude_version or 'unknown'}, Codex: 0.154.0"
    run_url = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/123"
    if result == "success":
        expected = f"✅ Latest harness test succeeded. {versions}. Link to run {run_url}"
    else:
        link = job_url if not api_error else run_url
        expected = (
            f"❌ ALERT! Latest harness test failed. {versions}. Link to failed job: {link} "
            "<@U09DPGC3DFY> <@U09EFHFRT24>"
        )
    assert posted == [{"text": expected}]
    assert expected in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="dedicated-cuj-plan runs on Linux")
def test_every_cuj_file_is_discovered_with_a_unique_check_name(tmp_path):
    root = Path(__file__).parent.parent
    entries, stderr = _discover_cujs(root, tmp_path / "github-output")

    assert entries is not None, stderr
    assert [entry["test"] for entry in entries] == sorted(
        path.name for path in (root / "tests/e2e_cuj").glob("test_*.py")
    )
    names = [entry["name"] for entry in entries]
    assert len(set(names)) == len(names), f"Duplicate CUJ_NAME values: {names}"
    for entry in entries:
        number = re.fullmatch(r"test_cuj(\d+)_[a-z0-9_]+\.py", entry["test"])
        assert number, f"Name CUJ files test_cuj<N>_<topic>.py: {entry['test']}"
        assert entry["name"].startswith(f"CUJ {number.group(1)} · "), entry


@pytest.mark.skipif(sys.platform == "win32", reason="dedicated-cuj-plan runs on Linux")
def test_cuj_discovery_fails_for_a_file_without_a_check_name(tmp_path):
    cujs = tmp_path / "tests/e2e_cuj"
    cujs.mkdir(parents=True)
    (cujs / "test_cuj1_a.py").write_text('import os\n\nCUJ_NAME = "CUJ 1 · A"\n')
    (cujs / "test_cuj2_b.py").write_text("import os\n")

    entries, stderr = _discover_cujs(tmp_path, tmp_path / "github-output")

    assert entries is None
    assert "test_cuj2_b.py must define CUJ_NAME" in stderr


FORK_GUARD = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)
# Fork PRs get no secrets or OIDC token, so jobs that need either must skip them.
SKIPPED_ON_FORK_PRS = {
    "workspace",
    "smoke",
    "headless-windows",
    "full",
    "soft-fail",
    "opencode",
    "copilot",
    "antigravity",
    "managed",
    "managed-opencode",
    "managed-copilot",
    "dedicated-cuj-plan",
    "dedicated-cuj",
    "cujs",
}


def _integration_jobs():
    workflow = (Path(__file__).parent.parent / ".github/workflows/integration.yml").read_text()
    body = workflow.split("\njobs:\n", 1)[1]
    return dict(re.findall(r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)", body))


def _skips_fork_prs(jobs, name):
    job = jobs[name]
    condition = re.search(r"(?m)^    if: (.*)$", job)
    if condition and FORK_GUARD in condition.group(1):
        return True
    needs = re.search(r"(?m)^    needs: \[?([a-z0-9-, ]+)\]?$", job)
    for need in [need.strip() for need in needs.group(1).split(",")] if needs else []:
        requires_success = (
            condition is None or f"needs.{need}.result == 'success'" in condition.group(1)
        )
        if requires_success and _skips_fork_prs(jobs, need):
            return True
    return False


def test_integration_jobs_that_run_on_fork_pull_requests_need_no_credentials():
    jobs = _integration_jobs()
    skipped = {name for name in jobs if _skips_fork_prs(jobs, name)}

    assert skipped == SKIPPED_ON_FORK_PRS
    for name in set(jobs) - skipped:
        assert "secrets." not in jobs[name] and "id-token: write" not in jobs[name], (
            f"{name} runs on fork PRs, which get no secrets or OIDC token"
        )


MEMBER_ONLY = 'contains(fromJSON(\'["MEMBER", "OWNER"]\'), github.event.comment.author_association)'


def _workflow(name):
    return (Path(__file__).parent.parent / ".github/workflows" / name).read_text()


def test_fork_integration_runs_only_member_requested_pr_heads():
    workflow = _workflow("fork-integration.yml")
    authorize = workflow.split("\n  authorize:\n", 1)[1].split("\n  reject:\n", 1)[0]
    reject = workflow.split("\n  reject:\n", 1)[1].split("\n  integration:\n", 1)[0]
    integration = workflow.split("\n  integration:\n", 1)[1].split("\n  report:\n", 1)[0]
    report = workflow.split("\n  report:\n", 1)[1]

    assert re.search(r"(?m)^on:\n  issue_comment:\n    types: \[created\]\n", workflow)
    assert "pull_request_target" not in workflow
    assert "actions/checkout" not in workflow, "Only integration.yml checks out the pinned commit"
    command = "startsWith(github.event.comment.body, '/integration-test ')"
    assert command in authorize and command in reject
    assert MEMBER_ONLY in authorize
    assert f"!{MEMBER_ONLY}" in reject
    assert "only Databricks org members can run integration tests" in reject
    assert "set -euo pipefail" in authorize
    assert '[[ "$sha" =~ ^[0-9a-f]{40}$ ]]' in authorize
    assert "state=pending" in authorize
    assert "needs: authorize" in integration
    assert "group: fork-integration-${{ github.event.issue.number }}" in integration
    assert "uses: ./.github/workflows/integration.yml" in integration
    assert "ref: ${{ needs.authorize.outputs.sha }}" in integration
    assert "needs.authorize.result == 'success'" in report
    assert "repos/$GITHUB_REPOSITORY/statuses/$SHA" in report
    assert "concurrency:" not in workflow.split("\njobs:\n", 1)[0], (
        "Workflow-level concurrency would let any PR comment cancel a run"
    )


HEAD = "0123456789abcdef0123456789abcdef01234567"
# Verified against the live API on the protected runner; a text change here must be re-verified.
PR_HEAD_JQ = '[.head.sha, (.head.repo.full_name // "")] | @tsv'


@pytest.mark.skipif(sys.platform == "win32", reason="the authorize step runs bash on Linux")
@pytest.mark.parametrize(
    ("comment", "head_repo", "accepted", "reason"),
    [
        ("/integration-test", "fork/unity-gateway", True, ""),
        (f"/integration-test {HEAD[:7]}", "fork/unity-gateway", True, ""),
        (f"/integration-test {HEAD}", "fork/unity-gateway", True, ""),
        ("/integration-test", "databricks/unity-gateway", True, ""),
        ("/integration-test 1234567", "fork/unity-gateway", False, "the PR head is now 0123456"),
        ("/integration-test please", "fork/unity-gateway", False, "use `/integration-test`"),
        ("/integration-test", None, False, "fork repository no longer exists"),
    ],
)
def test_fork_integration_authorize_pins_the_pr_head_or_the_reviewed_commit(
    tmp_path, comment, head_repo, accepted, reason
):
    workflow = _workflow("fork-integration.yml")
    authorize = workflow.split("\n  authorize:\n", 1)[1].split("\n  reject:\n", 1)[0]
    script = textwrap.dedent(authorize.split("        run: |\n", 1)[1])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # What real gh prints for that query; any other --jq expression gets no output and is refused.
    (tmp_path / "pr.tsv").write_text(f"{HEAD}\t{head_repo or ''}\n")
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{tmp_path}/calls"\n'
        f"case \"$*\" in 'api repos/databricks/unity-gateway/pulls/7 --jq {PR_HEAD_JQ}')"
        f' cat "{tmp_path}/pr.tsv";; esac\n'
    )
    fake_gh.chmod(0o755)
    output = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "databricks/unity-gateway",
            "GITHUB_OUTPUT": str(output),
            "PR_NUMBER": "7",
            "COMMENT_ID": "99",
            "COMMENT_BODY": comment,
            "RUN_URL": "https://example/run",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = (tmp_path / "calls").read_text()

    if accepted:
        assert result.returncode == 0, result.stdout + result.stderr
        assert output.read_text() == f"sha={HEAD}\n"
        assert f"statuses/{HEAD} -f state=pending" in calls
    else:
        assert result.returncode != 0
        assert not output.exists()
        assert reason in calls and "statuses/" not in calls


def test_fork_integration_github_api_jobs_run_on_allowlisted_runners():
    # The org IP allow list rejects GitHub API calls from GitHub-hosted runners.
    workflow = _workflow("fork-integration.yml")
    protected = "    runs-on:\n      group: databricks-protected-runner-group\n      labels: linux-ubuntu-latest\n"

    assert workflow.count(protected) == 3
    assert "runs-on: ubuntu-latest" not in workflow
    assert not re.search(r"(?m)^\s*[^#\n]*\bjq\s+-", workflow)


def test_no_workflow_uses_pull_request_target():
    # GitHub disables pull_request_target by default in public repos from 2026-11-02.
    workflows = sorted((Path(__file__).parent.parent / ".github/workflows").glob("*.yml"))

    assert workflows
    assert [path.name for path in workflows if "pull_request_target" in path.read_text()] == []


def test_user_journey_gate_runs_for_in_repo_prs_or_a_member_command():
    workflow = _workflow("user-journey-test-required.yml")
    trigger = workflow.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]

    assert "  pull_request:\n" in trigger and "  issue_comment:\n    types: [created]" in trigger
    assert "github.event.pull_request.head.repo.full_name == github.repository" in workflow
    assert "github.event.comment.body == '/user-journey-check'" in workflow
    assert MEMBER_ONLY in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert "concurrency:" not in workflow.split("\njobs:\n", 1)[0]


def test_user_journey_fork_report_uses_the_evaluated_commit():
    report = _workflow("user-journey-test-required.yml").split(
        "Report the result on the fork PR", 1
    )[1]

    assert "SHA: ${{ steps.gate.outputs.sha }}" in report
    assert "repos/$GITHUB_REPOSITORY/statuses/$sha" in report
    assert "/pulls/" not in report and ".head.sha" not in report


def test_ug_review_accepts_member_comments_and_in_repo_labels_with_trusted_checkout():
    workflow = _workflow("ug-review.yml")

    assert re.search(r"(?m)^  issue_comment:\n    types: \[created\]\n", workflow)
    assert re.search(r"(?m)^on:\n  pull_request:\n    types: \[labeled\]\n", workflow)
    assert (
        'contains(fromJSON(\'["ug-review", "code-review"]\'), github.event.label.name)' in workflow
    )
    assert "github.event.pull_request.head.repo.full_name == github.repository" in workflow
    assert "pull_request_target" not in workflow
    assert "github.event.comment.body == '/ug-review'" in workflow
    assert MEMBER_ONLY in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert (
        "PR_NUMBER: ${{ github.event.pull_request.number || github.event.issue.number }}"
        in workflow
    )
    assert "concurrency:" not in workflow.split("\njobs:\n", 1)[0]


def test_integration_checkouts_and_caches_are_safe_for_fork_heads():
    workflow = (Path(__file__).parent.parent / ".github/workflows/integration.yml").read_text()
    checkouts = re.findall(r"(?m)^      - uses: actions/checkout@.*\n((?:        .*\n)*)", workflow)
    caches = re.findall(r"(?m)^      - uses: astral-sh/setup-uv@.*\n((?:        .*\n)*)", workflow)

    assert checkouts and caches
    for block in checkouts:
        assert "ref: ${{ inputs.ref || github.sha }}" in block, block
        assert "persist-credentials: false" in block, block
    for block in caches:
        assert "enable-cache: ${{ inputs.ref == '' }}" in block, block


def test_windows_integration_ci_uses_shared_claude_version():
    workflow = Path(__file__).parent.parent / ".github/workflows/integration.yml"
    contents = workflow.read_text()

    assert "  CLAUDE_VERSION: ${{ inputs.claude_version || '2.1.290' }}" in contents
    assert contents.count('"--claude-version", $env:CLAUDE_VERSION,') == 2


@pytest.mark.parametrize(
    "failed_job",
    [
        "installation",
        "installation-windows",
        "headless-windows",
        "workspace",
        "smoke",
        "full",
        "antigravity",
        "managed",
        "dedicated-cuj-plan",
        "dedicated-cuj",
    ],
)
@pytest.mark.parametrize("job_result", ["success", "failure", "cancelled", "skipped"])
def test_integration_ci_gate_requires_every_job(failed_job, job_result):
    workflow = Path(__file__).parent.parent / ".github/workflows/integration.yml"
    gate = workflow.read_text().split("\n  cujs:\n", 1)[1]
    script = re.search(r"          python3 - <<'PY'\n(.*?)          PY", gate, re.DOTALL)
    assert script is not None
    results = {
        job: {"result": "success"}
        for job in (
            "installation",
            "installation-windows",
            "headless-windows",
            "workspace",
            "smoke",
            "full",
            "antigravity",
            "managed",
            "dedicated-cuj-plan",
            "dedicated-cuj",
        )
    }
    results[failed_job]["result"] = job_result
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script.group(1))],
        env={"RESULTS": json.dumps(results)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    if job_result == "success":
        assert result.returncode == 0, result.stderr
        assert "All integration jobs passed:" in result.stdout
    else:
        assert result.returncode != 0
        assert f"Integration jobs did not pass: {failed_job}" in result.stderr


def test_integration_suite_uses_only_public_process_boundaries():
    violations = []
    for path in (Path(__file__).parent / "integration").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            if any(module.split(".")[0] in {"ucode", "mock", "unittest"} for module in modules):
                violations.append(f"{path.name}:{node.lineno}: imports application or test doubles")
            if isinstance(node, ast.Name) and node.id in {
                "monkeypatch",
                "MonkeyPatch",
                "Mock",
                "MagicMock",
                "patch",
                "setattr",
                "delattr",
            }:
                violations.append(f"{path.name}:{node.lineno}: uses {node.id}")
            if isinstance(node, ast.Attribute) and node.attr in {
                "MonkeyPatch",
                "Mock",
                "MagicMock",
                "mock",
                "patch",
                "skip",
                "skipif",
                "xfail",
            }:
                violations.append(f"{path.name}:{node.lineno}: uses {node.attr}")
    assert not violations, "\n".join(violations)


def test_live_integration_cases_belong_to_exactly_one_ci_agent():
    for path in (Path(__file__).parent / "integration").glob("test_*.py"):
        tree = ast.parse(path.read_text())
        module_marks = _markers(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in node.targets
            )
        )
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                marks = module_marks | _markers(node.decorator_list)
                if marks & {"live", "managed_fixture", "workspace_switch"}:
                    assert (
                        len(marks & {"claude", "codex", "opencode", "copilot", "antigravity"}) == 1
                    ), node.name


def test_model_discovery_cases_match_current_launch_contract():
    root = Path(__file__).parent
    seen = []
    paths = [
        *root.glob("integration/test_ug_*_model_discovery.py"),
        *root.glob("e2e_cuj/test_cuj*_model_discovery.py"),
    ]
    for path in paths:
        source = path.read_text()
        assert "UG_ENABLE_MODEL_DISCOVERY" not in source, path.name
        tree = ast.parse(source)
        # Model locations are launch-only on main, never configure options.
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            args = [arg.value for arg in call.args if isinstance(arg, ast.Constant)]
            if args and args[0] == "configure":
                assert "--model-location" not in args, path.name
        module_marks = _markers(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in node.targets
            )
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            match = re.match(r"test_case_(\d{2})_", node.name)
            if match is None:
                continue
            case = int(match.group(1))
            seen.append(case)
            marks = module_marks | _markers(node.decorator_list)
            expected = {"managed_fixture"} if case <= 6 else {"live"}
            assert marks & {"managed_fixture", "live"} == expected, node.name
            assert marks & {"claude", "codex"} == ({"claude"} if case % 2 else {"codex"}), node.name
            assert not any(arg.arg == "configured" for arg in node.args.args), node.name
            for value in ast.walk(node):
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    artifact = re.match(r"case-(\d{2})-", value.value)
                    if artifact:
                        assert int(artifact.group(1)) == case, (node.name, value.value)
    # Repository scenario numbers are consecutive, independent of the external
    # design document. Configured/fresh variants share their scenario number.
    expected_cases = set(range(1, 15))
    assert set(seen) == expected_cases
    assert len(seen) == 24
    for case in expected_cases:
        assert seen.count(case) == (1 if 7 <= case <= 10 else 2), case


@pytest.mark.parametrize("payload", [{}, {"coding_agent_configs": []}, []])
def test_unmanaged_discovery_accepts_an_empty_config_listing(payload):
    assert_no_managed_config(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"coding_agent_configs": [{"name": "coding-agent-configs/admin-policy"}]},
        [{"name": "coding-agent-configs/admin-policy"}],
    ],
)
def test_unmanaged_discovery_reports_published_config(payload):
    with pytest.raises(AssertionError, match="coding-agent-configs/admin-policy"):
        assert_no_managed_config(payload)


@pytest.mark.parametrize("payload", [None, "invalid", {"coding_agent_configs": {}}, [None]])
def test_unmanaged_discovery_rejects_malformed_config_listings(payload):
    with pytest.raises(AssertionError, match="Invalid CodingAgentConfig listing"):
        assert_no_managed_config(payload)


def test_smoke_covers_hosted_custom_oauth_and_headless_for_both_agents():
    smoke = set()
    for path in (Path(__file__).parent / "integration").glob("test_*.py"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and "smoke" in _markers(node.decorator_list):
                smoke.add(node.name)
    assert smoke == {
        "test_ug_configure_claude_databricks",
        "test_ug_configure_codex_databricks",
        "test_ug_claude_custom_oauth_cli_boots",
        "test_ug_codex_custom_oauth_cli_boots",
        "test_ug_claude_headless_prompt_argument",
        "test_ug_codex_headless_prompt_argument",
    }


def test_integration_tests_describe_the_scenario_and_expected_result():
    root = Path(__file__).parent / "integration"
    violations = []
    for path in root.rglob("test_*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            description = ast.get_docstring(node) or ""
            if "Scenario:" not in description or "Expected:" not in description:
                violations.append(f"{path.name}:{node.lineno}: describe Scenario and Expected")
            if any(arg.arg == "configured" for arg in node.args.args):
                violations.append(f"{path.name}:{node.lineno}: setup must be visible in the test")
    assert not violations, "\n".join(violations)


def test_live_suites_use_fresh_id_for_random_ids():
    root = Path(__file__).parent
    helper = root / "integration/utils/evidence.py"
    offenders = [
        str(path.relative_to(root))
        for suite in ("integration", "e2e_cuj")
        for path in (root / suite).rglob("*.py")
        if path != helper and "uuid4(" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"Use fresh_id() from integration/utils/evidence.py: {offenders}"
