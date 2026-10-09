"""Keep the black-box suite independent of application internals and test doubles."""

import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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


def _workflow_job(name, next_name):
    workflow = WORKFLOW.read_text()
    return workflow.split(f"\n  {name}:\n", 1)[1].split(f"\n  {next_name}:\n", 1)[0]


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
    "installation-windows",
    "workspace",
    "smoke",
    "headless-windows",
    "full",
    "opencode",
    "managed",
    "managed-opencode",
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
    pr = {"head": {"sha": HEAD, "repo": {"full_name": head_repo} if head_repo else None}}
    (tmp_path / "pr.json").write_text(json.dumps(pr))
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{tmp_path}/calls"\n'
        f'case "$*" in "api repos/databricks/unity-gateway/pulls/7") cat "{tmp_path}/pr.json";; esac\n'
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


def test_ug_review_runs_only_on_member_command_from_the_default_branch():
    workflow = _workflow("ug-review.yml")

    assert re.search(r"(?m)^on:\n  issue_comment:\n    types: \[created\]\n", workflow)
    assert "pull_request_target" not in workflow
    assert "github.event.comment.body == '/ug-review'" in workflow
    assert MEMBER_ONLY in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert "PR_NUMBER: ${{ github.event.issue.number }}" in workflow
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
                    assert len(marks & {"claude", "codex", "opencode"}) == 1, node.name


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
