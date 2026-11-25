"""Keep the black-box suite independent of application internals and test doubles."""

import ast
import http.client
import importlib.util
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.integration.utils.harness import UserSession, clean_environment
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


def test_integration_model_ledger_captures_before_session_cleanup():
    source = (Path(__file__).parent / "integration/conftest.py").read_text()
    assert 'os.environ.get("UG_MODEL_LEDGER")' in source
    assert source.index("record_case_usage(user") < source.index('"cleanup-revert"')
    assert 'request.node.get_closest_marker("workspace_switch")' in source


def test_windows_desktop_experiment_keeps_visible_setup_before_the_restart_gate():
    source = Path(__file__).parent / "integration/test_ug_codex_desktop_windows.py"
    cases = source.read_text().split("    with WindowsApp(")[1:]
    assert len(cases) == 2
    for case in cases:
        setup = case.index("wait_for_windows_setup(app)")
        assert setup < case.index('app.click("ControlType.Button", "Continue")', setup)
        assert case.index("wait_for_windows_sandbox(app") < case.index(
            "app.wait_for_setup_restart("
        )
        assert case.index("app.wait_for_setup_restart(") < case.index(
            'app.mark_setup_phase("first-interaction-start")'
        )


def test_windows_uia_observer_is_compiled_before_desktop_launch():
    workflow = _workflow("integration.yml")
    windows_job = workflow.split("  codex-desktop-windows:\n", 1)[1]
    assert windows_job.index("Compile UIA diagnostic observer") < windows_job.index(
        "Complete a Codex turn in the desktop app"
    )
    observer = windows_job.split("      - name: Compile UIA diagnostic observer\n", 1)[1].split(
        "      - name: Complete a Codex turn", 1
    )[0]
    assert observer.count("UG_UIA_PROBE_ASSEMBLY=") == 1
    assert re.search(
        r"if \(Test-Path -LiteralPath \$observer\) \{\s*"
        r'"UG_UIA_PROBE_ASSEMBLY=\$observer" \| Out-File[^\n]*\n\s*\}',
        observer,
    )
    assert "shell: powershell" in observer


def test_unit_shards_upload_junit_evidence_even_when_a_test_fails():
    workflow = _workflow("ci.yml")
    report = workflow.split("      - &unit-report\n", 1)[1].split("\n\n  unit-windows:", 1)[0]
    assert "if: ${{ !cancelled() }}" in report
    assert "${{ runner.temp }}/junit.xml" in report


def test_quota_diagnostics_emit_only_rate_and_caller_permission_fields():
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    assert script.is_file()
    diagnostics = runpy.run_path(str(script))
    service = {
        "config": {
            "rate_limits": [
                {
                    "key": "RATE_LIMIT_KEY_SERVICE_PRINCIPAL",
                    "principal": "caller-id",
                    "requests": 4,
                },
                {"key": "RATE_LIMIT_KEY_SERVICE_PRINCIPAL", "principal": "other-id", "tokens": 8},
            ],
            "secret": "never-print",
        }
    }
    summary = diagnostics["summarize_service"](service, "caller-id")
    assert summary == [
        {"key": "RATE_LIMIT_KEY_SERVICE_PRINCIPAL", "requests": 4, "applies_to_caller": True},
        {"key": "RATE_LIMIT_KEY_SERVICE_PRINCIPAL", "tokens": 8, "applies_to_caller": False},
    ]
    assert "never-print" not in repr(summary)
    assert "caller-id" not in repr(summary)


def test_staging_provider_quota_reports_only_safe_rate_fields(monkeypatch):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    diagnostics = runpy.run_path(str(script))
    calls = []

    def read(host, token, path):
        calls.append(path)
        return 200, {
            "config": {
                "rate_limits": [
                    {"key": "SERVICE_PRINCIPAL", "principal": "caller-id-secret", "tokens": 1000}
                ],
                "secret": "private-secret",
            }
        }

    monkeypatch.setitem(diagnostics["provider_limits"].__globals__, "read_api", read)
    result = diagnostics["provider_limits"](
        "https://workspace", "token", "catalog.schema.service", "caller-id-secret"
    )
    assert calls == ["/api/2.1/unity-catalog/model-provider-services/catalog.schema.service"]
    assert result == {
        "status": 200,
        "rate_limits_present": True,
        "rate_limits": [{"key": "SERVICE_PRINCIPAL", "tokens": 1000, "applies_to_caller": True}],
    }
    assert "private" not in repr(result) and "caller-id-secret" not in repr(result)


def test_staging_provider_quota_does_not_treat_denial_as_no_limit(monkeypatch):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    provider_limits = runpy.run_path(str(script))["provider_limits"]
    monkeypatch.setitem(provider_limits.__globals__, "read_api", lambda *args: (403, None))
    assert provider_limits("https://workspace", "token", "catalog.schema.service", "caller") == {
        "status": 403,
        "rate_limits_present": None,
        "rate_limits": [],
    }


def test_staging_provider_quota_uses_existing_m2m_and_no_inference(monkeypatch, capsys):
    from types import SimpleNamespace

    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    provider_main = runpy.run_path(str(script))["main"]
    monkeypatch.setenv("UCODE_TEST_WORKSPACE", "https://private.example")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "private-client")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "private-secret")
    monkeypatch.setenv("UG_COWORK_PROVIDER", "catalog.schema.service")
    monkeypatch.setitem(
        sys.modules,
        "run_integration",
        SimpleNamespace(mint_m2m_token=lambda *args: "private-token"),
    )
    calls = []
    monkeypatch.setitem(
        provider_main.__globals__,
        "provider_limits",
        lambda host, token, provider, caller: (
            calls.append((host, token, provider, caller)) or {"status": 200}
        ),
    )
    provider_main(provider_only=True)
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out) == {"status": 200}


def test_quota_diagnostics_report_malformed_json_without_failing(monkeypatch):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    diagnostics = runpy.run_path(str(script))

    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response(b"not-json"))
    assert diagnostics["read_api"]("https://private.example", "token", "/api") == (
        "invalid-json",
        None,
    )


def test_quota_diagnostics_report_truncated_response_without_failing(monkeypatch):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    diagnostics = runpy.run_path(str(script))

    class Response(io.BytesIO):
        status = 200

        def read(self, *args):
            raise http.client.IncompleteRead(b"partial", 20)

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response())
    assert diagnostics["read_api"]("https://private.example", "token", "/api") == (
        "incomplete-read",
        None,
    )


@pytest.mark.parametrize("error", [ConnectionResetError("connection"), TimeoutError("timeout")])
def test_quota_diagnostics_report_connection_failures_without_failing(monkeypatch, error):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    diagnostics = runpy.run_path(str(script))

    def fail(request, timeout):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    assert diagnostics["read_api"]("https://private.example", "token", "/api") == (
        "connection-error",
        None,
    )


def test_quota_diagnostics_handle_absent_optional_rate_configuration():
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    diagnostics = runpy.run_path(str(script))
    assert diagnostics["summarize_service"]({"config": None}, "caller") == []
    assert diagnostics["summarize_endpoint"]({"ai_gateway": None}, "caller") == []


def test_quota_diagnostics_read_ai_gateway_endpoint_limits():
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    diagnostics = runpy.run_path(str(script))
    endpoint = {"ai_gateway": {"rate_limits": [{"key": "endpoint", "calls": 9}]}}
    assert diagnostics["summarize_endpoint"](endpoint, "caller-id") == [
        {"key": "endpoint", "calls": 9}
    ]


def test_quota_diagnostics_use_m2m_and_do_not_print_credentials(monkeypatch, capsys):
    from types import SimpleNamespace

    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    main = runpy.run_path(str(script))["main"]
    monkeypatch.setenv("UCODE_TEST_WORKSPACE", "https://private.example")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "private-id")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "private-secret")
    monkeypatch.setitem(
        sys.modules,
        "run_integration",
        SimpleNamespace(mint_m2m_token=lambda host, caller, secret: "private-token"),
    )
    monkeypatch.setitem(main.__globals__, "read_api", lambda host, token, path: (403, None))
    monkeypatch.setitem(main.__globals__, "probe_model", lambda *args: [])
    main()
    output = capsys.readouterr().out
    assert "model_service_status" in output and "403" in output
    for value in ("private.example", "private-id", "private-secret", "private-token"):
        assert value not in output


@pytest.mark.parametrize(("config", "present"), [({}, False), ({"rate_limits": []}, True)])
def test_quota_diagnostics_distinguish_omitted_from_empty_limits(
    monkeypatch, capsys, config, present
):
    from types import SimpleNamespace

    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    main = runpy.run_path(str(script))["main"]
    monkeypatch.setenv("UCODE_TEST_WORKSPACE", "https://private.example")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "caller")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret")
    monkeypatch.setitem(
        sys.modules, "run_integration", SimpleNamespace(mint_m2m_token=lambda *args: "token")
    )
    monkeypatch.setitem(
        main.__globals__, "read_api", lambda host, token, path: (200, {"config": config})
    )
    monkeypatch.setitem(main.__globals__, "probe_model", lambda *args: [])
    main()
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["model_service_rate_limits_present"] is present


def test_claude_quota_diagnostics_include_models_used_by_managed_fixtures(monkeypatch, capsys):
    from types import SimpleNamespace

    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    main = runpy.run_path(str(script))["main"]
    monkeypatch.setenv("UCODE_TEST_WORKSPACE", "https://private.example")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "caller")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret")
    monkeypatch.setitem(
        sys.modules, "run_integration", SimpleNamespace(mint_m2m_token=lambda *args: "token")
    )
    monkeypatch.setitem(main.__globals__, "read_api", lambda *args: (403, None))
    monkeypatch.setitem(main.__globals__, "probe_model", lambda *args: [])
    main()
    models = {json.loads(line)["model"] for line in capsys.readouterr().out.splitlines()}
    assert {"system.ai.claude-opus-4-8", "system.ai.claude-sonnet-4-6"} <= models


def test_quota_probe_runs_exactly_three_concurrent_requests_and_redacts(monkeypatch):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    probe = runpy.run_path(str(script))["probe_model"]
    barrier = threading.Barrier(3)
    requests = []

    class Response(io.BytesIO):
        status = 200
        headers = {
            "x-request-id": "request-id",
            "x-ratelimit-remaining": "3",
            "authorization": "secret",
        }

    def urlopen(request, timeout):
        requests.append(request)
        barrier.wait(timeout=5)
        return Response(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    results = probe("https://private.example", "secret-token", "gpt-6-sol", "codex")
    assert len(requests) == len(results) == 3
    assert all(row["status"] == 200 and row["latency_ms"] >= 0 for row in results)
    assert all(
        row["headers"] == {"x-request-id": "request-id", "x-ratelimit-remaining": "3"}
        for row in results
    )
    assert all(request.full_url.endswith("/ai-gateway/codex/v1/responses") for request in requests)
    assert all(json.loads(request.data)["model"] == "system.ai.gpt-6-sol" for request in requests)
    assert "private.example" not in repr(results) and "secret" not in repr(results)


def test_quota_probe_rejects_unknown_agent_without_sending_requests(monkeypatch):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    probe = runpy.run_path(str(script))["probe_model"]
    requests = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: requests.append(args))
    with pytest.raises(ValueError, match="agent"):
        probe("https://private.example", "secret-token", "gpt-6-sol", "unknown")
    assert not requests


def test_quota_probe_marks_truncated_response_without_claiming_success(monkeypatch):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    probe = runpy.run_path(str(script))["probe_model"]

    class Response(io.BytesIO):
        status = 200
        headers = {"x-request-id": "request-id"}

        def read(self, *args):
            raise http.client.IncompleteRead(b"partial", 20)

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response())
    results = probe("https://private.example", "secret-token", "gpt-6-sol", "codex")
    assert len(results) == 3
    assert all(row["status"] == 200 and row["error"] == "incomplete-read" for row in results)


def test_quota_probe_records_connection_errors_without_retry(monkeypatch):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    probe = runpy.run_path(str(script))["probe_model"]
    requests = []

    def disconnected(request, timeout):
        requests.append(request)
        raise ConnectionResetError("private diagnostic")

    monkeypatch.setattr(urllib.request, "urlopen", disconnected)
    results = probe("https://private.example", "secret-token", "gpt-6-sol", "codex")
    assert len(requests) == len(results) == 3
    assert all(row["status"] == "transport-error" for row in results)
    assert "private diagnostic" not in repr(results)


def test_quota_probe_never_retries_429(monkeypatch):
    script = Path(__file__).parent.parent / "scripts/managed_quota_diagnostics.py"
    probe = runpy.run_path(str(script))["probe_model"]
    requests = []

    def rejected(request, timeout):
        requests.append(request)
        raise urllib.error.HTTPError(request.full_url, 429, "limited", {"Retry-After": "9"}, None)

    monkeypatch.setattr(urllib.request, "urlopen", rejected)
    results = probe("https://private.example", "secret-token", "gpt-5-6-sol", "codex")
    assert len(requests) == len(results) == 3
    assert all(row["status"] == 429 and row["headers"]["retry-after"] == "9" for row in results)


def test_managed_quota_probe_runs_once_after_gate_without_blocking_e2e():
    ci = _workflow("ci.yml").split("\njobs:\n", 1)[1]
    jobs = dict(re.findall(r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)", ci))
    job = jobs["managed-quota-probe"]
    assert "needs: [security-gate, integration]" in job
    assert _requires_gate_success(job)
    assert (
        "if: ${{ always() && !cancelled() && needs.security-gate.result == 'success' && "
        "(github.event_name == 'workflow_dispatch' || github.event_name == 'schedule') }}"
    ) in job
    assert "continue-on-error: true" in job
    assert "timeout-minutes: 10" in job
    assert "python scripts/managed_quota_diagnostics.py" in job
    assert "UCODE_TEST_WORKSPACE: ${{ secrets.E2E_ADMIN_WORKSPACE }}" in job
    assert "DATABRICKS_CLIENT_ID: ${{ secrets.E2E_ADMIN_SP_CLIENT_ID }}" in job
    assert "DATABRICKS_CLIENT_SECRET: ${{ secrets.E2E_ADMIN_SP_CLIENT_SECRET }}" in job
    for name in ("e2e", "integration", "required-e2e"):
        assert "managed-quota-probe" not in jobs[name]
    for name in ("managed", "codex-desktop-macos", "codex-desktop-windows"):
        assert "managed-quota-diagnostics" not in _integration_jobs()[name]


def test_isolated_sessions_disable_antigravity_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", "1")

    assert clean_environment(tmp_path)["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


def test_isolated_sessions_capture_proxy_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("UCODE_RELAYED_PROXY_DIAGNOSTICS", "0")
    root, project = tmp_path / "session", tmp_path / "project"
    root.mkdir()
    project.mkdir()
    session = UserSession(root, project, Path(sys.executable), tmp_path / "artifacts")

    started_ms = int(time.time() * 1000)
    result = session.run(
        "-c",
        "from ucode.gateway_proxy import log_proxy_diagnostic; "
        "log_proxy_diagnostic('request_start', request_id='diagnostic-check')",
    )

    prefix, payload = result.stderr.strip().split(" ", 1)
    assert prefix == "[ucode-relay]"
    event = json.loads(payload)
    assert started_ms <= event.pop("timestamp_ms") <= int(time.time() * 1000)
    assert event == {"event": "request_start", "request_id": "diagnostic-check"}
    recorded = json.loads((session.artifacts / "command-1.json").read_text())
    assert recorded["stderr"] == result.stderr


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


def test_model_ledger_case_directories_do_not_collide():
    jobs = _integration_jobs()
    for job_name in ("managed-smart", "codex-desktop-macos", "codex-desktop-windows"):
        cases = _collect_integration_cases(_ci_marker_expression(jobs[job_name], "codex"))
        names = [re.sub(r"[^a-zA-Z0-9_.-]", "_", case.split("::")[-1]) for case in cases]
        assert len(names) == len(set(names)), job_name


def test_managed_smart_old_codex_pin_matches_the_version_aware_test():
    job = _integration_jobs()["managed"]
    pinned = re.search(r"@openai/codex@(\d+)\.(\d+)\.(\d+)", job)
    assert pinned, "The managed lane must pin the side-by-side old Codex"
    source = (Path(__file__).parent / "integration/test_ug_smart_routing_hooks.py").read_text()
    expected = re.search(r"assert installed_version == \((\d+), (\d+), (\d+)\)", source)
    assert expected, "The version-aware test must pin the installed old Codex"
    assert tuple(map(int, pinned.groups())) == tuple(map(int, expected.groups()))


def test_managed_smart_uses_a_cached_side_by_side_old_codex():
    job = _integration_jobs()["managed"]
    assert "Cache Codex 0.156.0" in job
    assert "codex-old-${{ runner.os }}-${{ runner.arch }}-node-22.19.0-0.156.0" in job
    assert "@openai/codex@0.156.0" in job
    assert "UG_INTEGRATION_OLD_CODEX_BIN_DIR" in job
    assert "env.UG_MODEL_LEDGER == 'smart'" in job


def test_managed_smart_overlap_keeps_model_guard_and_required_rollup():
    jobs = _integration_jobs()
    assert not re.search(r"(?m)^    needs:", jobs["managed-smart"])
    assert not re.search(r"(?m)^    needs:", jobs["codex-desktop-macos"])
    assert "if: ${{ !cancelled() && " in jobs["managed-smart"]
    assert "if: ${{ !cancelled() && " in jobs["codex-desktop-macos"]
    assert "needs: codex-desktop-macos" in jobs["codex-desktop-windows"]
    assert "max-parallel: 1" not in jobs["managed"]
    assert "managed-smart" in re.search(r"(?m)^    needs: (.*)$", jobs["cujs"]).group(1)
    assert "UG_MODEL_LEDGER: smart" in jobs["managed-smart"]
    assert "steps: *managed-steps" in jobs["managed-smart"]
    assert "Managed config · Codex · Smart routing'" in jobs["managed-smart"]
    assert "format('Managed config · {0}'" in jobs["managed"]
    for lane in ("codex-desktop-macos", "codex-desktop-windows"):
        assert "UG_MODEL_LEDGER: desktop" in jobs[lane]
        assert "--lane desktop" in jobs[lane]
    assert '--lane "$UG_MODEL_LEDGER"' in jobs["managed"]


def test_managed_integration_ci_is_blocking():
    jobs = _integration_jobs()
    managed, smart, gate = jobs["managed"], jobs["managed-smart"], jobs["cujs"]
    assert "continue-on-error:" not in managed and "continue-on-error:" not in smart
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", gate)
    assert needs is not None
    assert {"managed", "managed-smart"} <= {job.strip() for job in needs.group(1).split(",")}
    assert (
        "if: ${{ inputs.mode != 'advisory' && always() && (github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository) }}"
    ) in gate
    baseline = _collect_integration_cases(_ci_marker_expression(managed, "codex"))
    routing = _collect_integration_cases(_ci_marker_expression(smart, "codex"))
    assert not baseline & routing
    assert baseline | routing == _collect_integration_cases(
        "(managed_fixture or workspace_switch) and codex and not soft_fail"
    )
    equals_model_case = (
        "test_ug_codex_v2_routing.py::"
        "test_ug_codex_native_v2_plain_assignment_routing[managed-equals]"
    )
    assert equals_model_case not in baseline
    assert equals_model_case in routing
    fallback = (
        "test_ug_configure_managed_models.py::test_ug_configure_managed_codex_catalog_fallback"
    )
    assert f"{fallback}[routing-off]" in baseline
    assert f"{fallback}[routing-on]" in routing
    assert (
        "test_ug_configure_managed_models.py::test_managed_fixture_codex_smart_routing_banner"
        in routing
    )
    version_aware = (
        "test_ug_smart_routing_hooks.py::"
        "test_smart_router_managed_default_matches_codex_version_at_launch"
    )
    assert version_aware not in baseline
    assert version_aware in routing
    assert version_aware in _collect_integration_cases(
        "harness_latest and codex and managed_fixture"
    )
    assert (
        "test_ug_smart_routing_hooks.py::"
        "test_smart_router_managed_default_routes_supported_codex_at_launch"
    ) in routing


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
    assert "--desktop-app" in job
    assert _collect_integration_cases(_ci_marker_expression(job, "codex")) == {
        "test_ug_codex_desktop_macos.py::test_ug_codex_desktop_macos_completes_a_gateway_turn",
        "test_ug_codex_desktop_macos.py::test_ug_codex_desktop_macos_skill_calls_mcp",
    }


def test_codex_desktop_windows_ci_is_blocking_and_installs_a_pinned_app():
    jobs = _integration_jobs()
    job = jobs["codex-desktop-windows"]
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", jobs["cujs"])
    assert needs is not None
    assert "codex-desktop-windows" in {name.strip() for name in needs.group(1).split(",")}
    assert "runs-on: windows-latest" in job
    assert "continue-on-error:" not in job
    assert re.search(r"(?m)^      CHATGPT_APP_VERSION: \d+\.\d+\.\d+\.\d+$", job)
    assert re.search(r"(?m)^      CHATGPT_APP_SHA256: [0-9a-f]{64}$", job)
    assert "codex-app-prod/releases/$env:CHATGPT_APP_VERSION/ChatGPT-x64.msix" in job
    assert "-ne $env:CHATGPT_APP_SHA256" in job
    assert "--desktop-app" in job
    assert _collect_integration_cases(_ci_marker_expression(job, "codex")) == {
        "test_ug_codex_desktop_windows.py::test_ug_codex_desktop_windows_completes_a_gateway_turn",
        "test_ug_codex_desktop_windows.py::test_ug_codex_desktop_windows_skill_calls_mcp",
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
    assert "${{ matrix.suite == 'desktop' && '--desktop-app' || '' }}" in workflow
    assert _collect_integration_cases(entry.group(2)) == {
        "test_ug_codex_desktop_macos.py::test_ug_codex_desktop_macos_completes_a_gateway_turn",
        "test_ug_codex_desktop_macos.py::test_ug_codex_desktop_macos_skill_calls_mcp",
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
    diagnostic_steps = {
        "Record staging model usage",
        "Inspect staging provider rate limits",
        "Record staging Desktop model usage",
    }
    for name in ("smoke", "full", "managed"):
        assert not re.search(r"(?m)^    continue-on-error:", jobs[name])
        for step in re.split(r"(?m)^      - ", jobs[name])[1:]:
            if "continue-on-error:" in step:
                assert step.splitlines()[0].removeprefix("name: ") in diagnostic_steps
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
        assert re.search(r"codex_version:\n\s+description: [^\n]+\n\s+default: 0\.156\.0", workflow)
        assert "  CODEX_VERSION: ${{ inputs.codex_version || '0.156.0' }}" in workflow
    jobs = _integration_jobs()
    selected = []
    for name in ("smoke", "full"):
        selected.append(_collect_integration_cases(_ci_marker_expression(jobs[name], agent)))
    smoke, full = selected
    if agent == "codex":
        routing = _collect_integration_cases(
            _ci_marker_expression(jobs["full"], agent, smart_routing=True)
        )
        managed_routing = _collect_integration_cases(
            _ci_marker_expression(jobs["managed-smart"], agent)
        )
        assert not (smoke | full) & routing
        equals_model_case = (
            "test_ug_codex_v2_routing.py::"
            "test_ug_codex_native_v2_plain_assignment_routing[managed-equals]"
        )
        assert equals_model_case not in routing
        assert equals_model_case in managed_routing
        for model_option in ("saved", "split"):
            assert (
                "test_ug_codex_v2_routing.py::test_ug_codex_native_v2_plain_assignment_routing"
                f"[{model_option}]" in routing
            )
        assert any("::test_smart_routing_codex_route_subagent_hook[" in case for case in full)
        assert (
            "CODEX_VERSION: ${{ matrix.smart_routing && '0.171.0' || "
            "inputs.codex_version || '0.156.0' }}"
        ) in jobs["full"]
        assert "matrix.smart_routing && '-smart-routing' || ''" in jobs["full"]
        assert "CODEX_VERSION: '0.171.0'" in jobs["managed-smart"]
        assert "CODEX_VERSION: ${{ inputs.codex_version || '0.156.0' }}" in jobs["managed"]
        for platform in ("ubuntu-22.04", "windows-latest"):
            assert (
                f"- os: {platform}\n            agent: codex\n            smart_routing: true"
                in jobs["full"]
            )
        assert "AGENT: codex" in jobs["managed-smart"]
        full |= routing
        relay = _collect_integration_cases(_ci_marker_expression(jobs["codex-relay"], agent))
        assert relay == {
            "test_ug_codex_relayed.py::test_ug_codex_relayed_completes_inference_through_the_relay"
        }
        assert not (smoke | full) & relay
        full |= relay
    # Full skips smoke cases only because smoke runs them on the same OS matrix.
    assert "os: [ubuntu-22.04, windows-latest]" in jobs["smoke"]
    for platform in ("ubuntu-22.04", "windows-latest"):
        for lane_agent in ("claude", "codex"):
            assert f"- os: {platform}\n            agent: {lane_agent}" in jobs["full"]
    assert not smoke & full
    selected = smoke | full
    if agent == "claude":
        staging = _collect_integration_cases(
            f"({_ci_marker_expression(jobs['anthropic-mps'], agent)}) and live"
        )
        assert not selected & staging
        selected |= staging
    assert selected == _collect_integration_cases(f"live and {agent} and not soft_fail")


def test_codex_relay_ci_uses_allowlisted_runner_and_blocks_gate():
    jobs = _integration_jobs()
    relay = jobs["codex-relay"]
    assert "group: databricks-protected-runner-group" in relay
    assert "labels: linux-ubuntu-latest" in relay
    assert "CODEX_ACCESS_TOKEN: ${{ secrets.CODEX_OAUTH_TOKEN }}" in relay
    assert "CODEX_ACCESS_TOKEN" not in jobs["full"]
    assert "PACKAGE_INDEX: https://pypi-proxy.cloud.databricks.com/simple" in relay
    assert "NPM_REGISTRY: https://npm-proxy.cloud.databricks.com" in relay
    assert "steps: *live-steps" in relay
    assert "continue-on-error:" not in relay
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", jobs["cujs"])
    assert needs is not None
    assert "codex-relay" in {name.strip() for name in needs.group(1).split(",")}


def test_anthropic_mps_ci_uses_staging_service_principal_and_preserves_coverage(monkeypatch):
    monkeypatch.setenv("UG_INTEGRATION_AGENTS", "claude,codex,copilot")
    jobs = _integration_jobs()
    job = jobs["anthropic-mps"]
    assert not re.search(r"(?m)^    needs:", job)
    assert "UCODE_TEST_WORKSPACE: https://dbc-1bb9e8f5-bf3a.staging.cloud.databricks.com" in job
    assert "DATABRICKS_CLIENT_ID: 39206fba-a07b-4954-b8f9-4a1c9c069bba" in job
    assert "DATABRICKS_CLIENT_SECRET: ${{ secrets.UG_ANTHROPIC_MPS_CLIENT_SECRET }}" in job
    assert "DATABRICKS_BEARER:" not in job
    # The blocking caller runs the sharded Claude legs; the advisory caller the Copilot legs.
    assert "inputs.mode == 'required' &&" in job
    assert "inputs.mode == 'advisory' &&" in job
    assert '"agent":"claude","shard":"1/3"' in job
    assert '"agent":"copilot"' in job
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
    assert (
        "timeout-minutes: ${{ matrix.agent == 'copilot' && 30 || "
        "matrix.os == 'windows-latest' && 85 || 40 }}"
    ) in job
    assert "AGENT: ${{ matrix.agent }}" in job
    assert (
        "ARTIFACT_NAME: integration-anthropic-mps-${{ matrix.agent }}-${{ matrix.os }}"
        "${{ matrix.shard && '-shard' || '' }}${{ matrix.shard_index || '' }}" in job
    )
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


def test_staging_usage_and_provider_quota_are_collected_without_inference():
    jobs = _integration_jobs()
    staging = jobs["anthropic-mps"]
    assert "UG_MODEL_LEDGER: ${{ matrix.agent == 'claude' && 'staging' || '' }}" in staging
    steps = jobs["smoke"]
    assert "name: Record staging model usage" in steps
    assert "--lane staging" in steps
    assert "name: Inspect staging provider rate limits" in steps
    assert "scripts/managed_quota_diagnostics.py --provider-only" in steps
    assert "name: Record staging Desktop model usage" in steps
    assert "--lane staging-desktop" in steps
    assert "configuration.json" in steps
    assert steps.index("name: Record staging Desktop model usage") < steps.index(
        "name: Upload Cowork evidence"
    )


def test_staging_windows_node_toolcache_is_restored_before_setup():
    jobs = _integration_jobs()
    staging = jobs["anthropic-mps"]
    assert (
        "UG_CACHE_PINNED_NODE: ${{ inputs.ref == '' && matrix.os == 'windows-latest' "
        "&& matrix.agent == 'claude' }}" in staging
    )
    live_steps = jobs["smoke"]
    restore = live_steps.index("name: Restore pinned Node toolcache")
    setup = live_steps.index("uses: actions/setup-node@", restore)
    save = live_steps.index("name: Save pinned Node toolcache", setup)
    assert restore < setup < save
    segment = live_steps[restore:save]
    assert "node-version: 22.19.0" in segment
    assert "actions/cache/restore@" in segment
    assert "if: ${{ env.UG_CACHE_PINNED_NODE == 'true' }}" in segment
    assert live_steps.count("path: ${{ runner.tool_cache }}/node/22.19.0") == 2
    assert live_steps.count("key: node-toolcache-${{ runner.os }}-${{ runner.arch }}-22.19.0") == 2
    assert "actions/cache/save@" in live_steps[save:]
    assert "steps.node-toolcache.outputs.cache-hit != 'true'" in live_steps[save:]


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
    assert "max-parallel:" not in jobs["full"]
    assert "needs: workspace" in jobs["full"]
    assert "not smoke" in _ci_marker_expression(jobs["full"], "claude")
    assert "not smoke" in _ci_marker_expression(jobs["full"], "codex")
    assert "UCODE_TEST_WORKSPACE: ${{ secrets.UCODE_TEST_WORKSPACE }}" in jobs["full"]
    assert "DATABRICKS_BEARER: ${{ secrets.DATABRICKS_BEARER }}" in jobs["full"]
    assert "E2E_ADMIN_SP_CLIENT_ID" not in jobs["full"]


def test_full_claude_lane_includes_both_desktop_journeys():
    full = _integration_jobs()["full"]
    cases = _collect_integration_cases(_ci_marker_expression(full, "claude"))
    assert {
        "test_ug_configure_claude_desktop.py::test_ug_configure_claude_desktop_profile_lifecycle",
        "test_ug_configure_claude_desktop.py::test_ug_configure_claude_desktop_subscription_relay",
    } <= cases
    for platform in ("ubuntu-22.04", "windows-latest"):
        assert f"- os: {platform}\n            agent: claude" in full
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
        (
            "managed-smart",
            "codex",
            True,
            "test_smart_router_skill_turns_codex_subagent_routing_back_on",
        ),
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
    script = textwrap.dedent(plan.split("        run: |\n", 1)[1].split("\n      - ", 1)[0])
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
    assert "format('E2E CUJs · {0}', matrix.name)" in job
    assert "fail-fast: false" in job
    assert "include: ${{ fromJSON(needs.dedicated-cuj-plan.outputs.cujs) }}" in job
    assert 'pytest --confcutdir=tests/e2e_cuj "tests/e2e_cuj/${{ matrix.test }}"' in job
    assert 'UV_HTTP_TIMEOUT: "120"' in job and 'UV_HTTP_RETRIES: "6"' in job
    assert "UG_CUJ1_WORKSPACE: ${{ secrets.UG_CUJ1_WORKSPACE }}" in job
    assert '"@github/copilot@$COPILOT_VERSION"' in job
    assert (
        "CODEX_VERSION: ${{ (matrix.test == 'test_cuj4_smart_routing.py' || "
        "matrix.test == 'test_cuj7_model_discovery.py') && '0.171.0' || "
        "inputs.codex_version || '0.156.0' }}"
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
        "CODEX_VERSION": "0.156.0",
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
    versions = f"Versions used: Claude Code {claude_version or 'unknown'}, Codex: 0.156.0"
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
    files = sorted(path.name for path in (root / "tests/e2e_cuj").glob("test_*.py"))
    assert sorted({entry["test"] for entry in entries}) == files
    assert len({entry["name"] for entry in entries}) == len(files), (
        f"Duplicate CUJ_NAME values: {[entry['name'] for entry in entries]}"
    )
    for entry in entries:
        number = re.fullmatch(r"test_cuj(\d+)_[a-z0-9_]+\.py", entry["test"])
        assert number, f"Name CUJ files test_cuj<N>_<topic>.py: {entry['test']}"
        assert entry["name"].startswith(f"CUJ {number.group(1)} · "), entry
    # A file recorded with several shards expands to that many matrix entries, 1/N through N/N.
    recorded = json.loads((root / "tests/live_test_times.json").read_text())
    for path in files:
        shards = recorded.get(f"cuj-{path}", {}).get("Linux", {}).get("shards", 1)
        legs = [entry for entry in entries if entry["test"] == path]
        assert len(legs) == shards
        for entry in legs:
            if shards > 1:
                index, _, total = entry["shard"].partition("/")
                assert (int(index), int(total)) == (legs.index(entry) + 1, shards)
                assert entry["shard_index"] == legs.index(entry) + 1
            else:
                assert "shard" not in entry and "shard_index" not in entry


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
    "codex-relay",
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
    "vscode",
    "managed",
    "managed-smart",
    "managed-opencode",
    "codex-desktop-macos",
    "codex-desktop-windows",
    "dedicated-cuj-plan",
    "dedicated-cuj",
    "live-shard-times",
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
        expression = condition.group(1) if condition else ""
        requires_success = f"needs.{need}.result == 'success'" in expression or not re.search(
            r"\b(?:always|cancelled|failure|success)\(\)", expression
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
    assert f"!{MEMBER_ONLY}" in reject
    # Integration runs PR code with CI secrets, so a contributor can't start it on their own PR.
    assert "CONTRIBUTOR" not in workflow
    assert "only Databricks org members can run integration tests" in reject
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


def _requires_gate_success(header):
    condition = re.search(r"(?m)^    if: (.*)$", header)
    if not condition:
        return False
    expression = condition[1].strip().removeprefix("${{").removesuffix("}}").strip()
    terms = []
    depth = 0
    quoted = False
    start = 0
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "'":
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return False
            elif depth == 0 and expression[index : index + 2] in ("&&", "||"):
                if expression[index : index + 2] == "||":
                    return False
                terms.append(expression[start:index].strip())
                index += 1
                start = index + 1
        index += 1
    terms.append(expression[start:].strip())
    return not quoted and depth == 0 and "needs.security-gate.result == 'success'" in terms


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (
            "github.event_name != 'merge_group' && (github.event_name != 'pull_request' || !github.event.pull_request.draft) && needs.security-gate.result == 'success' && !cancelled()",
            True,
        ),
        ("needs.security-gate.result == 'success' || true", False),
        ("always() || needs.security-gate.result == 'success'", False),
        ("success() || needs.security-gate.result == 'success'", False),
        ("needs.security-gate.result == 'success' && success() || true", False),
        ("(needs.security-gate.result == 'success' || true) && success()", False),
        ("(needs.security-gate.result == 'success' && success()) || true", False),
    ],
)
def test_gate_success_condition_requires_a_top_level_guard(expression, expected):
    assert _requires_gate_success(f"    if: ${{{{ {expression} }}}}\n") is expected


def test_pr_jobs_with_credentials_require_a_successful_security_gate():
    workflows = Path(__file__).parent.parent / ".github/workflows"
    credential = re.compile(
        r"secrets\s*[.\[]|(?m:^\s*secrets:)|(?m:^\s*environment:)|"
        r"(?m:^\s*id-token:\s*write\b)|"
        r"\b(?:DATABRICKS_CLIENT_ID|DATABRICKS_CLIENT_SECRET|DATABRICKS_BEARER|"
        r"[A-Z0-9_]*SP_CLIENT_(?:ID|SECRET))\b"
    )
    # The secret-bearing integration workflow runs PR code only through these two callers.
    callers = {
        path.name
        for path in workflows.glob("*.yml")
        if "uses: ./.github/workflows/integration.yml" in path.read_text()
    }
    assert callers == {"ci.yml", "fork-integration.yml"}
    for workflow in callers:
        text = _workflow(workflow)
        header, body = text.split("\njobs:\n", 1)
        assert not credential.search(header), f"{workflow}: workflow-wide credentials"
        jobs = dict(
            re.findall(r"(?ms)^  ([A-Za-z0-9_-]+):\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", body)
        )
        for name, job in jobs.items():
            if name == "security-gate":
                assert not credential.search(job), (workflow, name)
                continue
            has_pr_token = workflow == "ci.yml" and "github.token" in job
            has_pr_write = workflow == "ci.yml" and re.search(
                r"(?m)^\s+[a-z][a-z-]*:\s*write\b", job
            )
            if (
                not credential.search(job)
                and not has_pr_token
                and not has_pr_write
                and "uses: ./.github/workflows/" not in job
            ):
                continue
            needs = re.search(r"(?m)^    needs: \[?([a-z0-9-, ]+)\]?$", job)
            assert needs and "security-gate" in needs[1].split(", "), (workflow, name)
            # GitHub's implicit success() is overridden by these status functions.
            job_header = job.split("\n    steps:", 1)[0]
            if (
                re.search(r"\b(?:always|failure|cancelled|success)\s*\(", job_header)
                or "needs.security-gate.result" in job_header
            ):
                assert _requires_gate_success(job_header), (workflow, name)

    ci = _workflow("ci.yml").split("\njobs:\n", 1)[1]
    jobs = dict(re.findall(r"(?ms)^  ([A-Za-z0-9_-]+):\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", ci))
    for name, job in jobs.items():
        if name == "security-gate":
            continue
        needs = re.search(r"(?m)^    needs: \[?([a-z0-9-, ]+)\]?$", job)
        assert needs and "security-gate" in needs[1].split(", "), name
    for name in ("unit-test-times", "e2e"):
        assert _requires_gate_success(jobs[name]), name
    for name in (
        "checks",
        "unit-linux",
        "unit-windows",
        "windows-provisioning",
        "vscode-extension",
    ):
        job = re.sub(
            r"(?m)^[ \t]*(?:DATABRICKS_CLIENT_ID|DATABRICKS_CLIENT_SECRET|DATABRICKS_BEARER|"
            r"[A-Z0-9_]*SP_CLIENT_(?:ID|SECRET)): ''$",
            "",
            jobs[name],
        )
        assert not credential.search(job), name


def test_credential_contract_rejects_replacing_an_empty_budget_secret(monkeypatch):
    workflow = _workflow

    def injected(name):
        text = workflow(name)
        if name == "ci.yml":
            text = text.replace(
                "DATABRICKS_BEARER: ''", "DATABRICKS_BEARER: ${{ secrets.TOKEN }}", 1
            )
        return text

    monkeypatch.setitem(
        test_pr_jobs_with_credentials_require_a_successful_security_gate.__globals__,
        "_workflow",
        injected,
    )
    with pytest.raises(AssertionError, match="checks"):
        test_pr_jobs_with_credentials_require_a_successful_security_gate()


@pytest.mark.parametrize(
    "prerequisites",
    [
        "",
        "    needs: security-gate\n    if: ${{ success() || true }}\n",
        "    needs: security-gate\n    if: ${{ needs.security-gate.result == 'success' || true }}\n",
        "    needs: security-gate\n    if: ${{ always() || needs.security-gate.result == 'success' }}\n",
        "    needs: security-gate\n    if: ${{ needs.security-gate.result == 'success' && success() || true }}\n",
        "    needs: security-gate\n    if: ${{ (needs.security-gate.result == 'success' || true) && success() }}\n",
    ],
)
def test_credential_contract_rejects_a_new_ungated_job(monkeypatch, prerequisites):
    workflow = _workflow
    ci = workflow("ci.yml") + (
        "\n  accidental-secret:\n    runs-on: ubuntu-latest\n"
        + prerequisites
        + "    env:\n      TOKEN: ${{ secrets.CI_TOKEN }}\n"
    )
    monkeypatch.setitem(
        globals(), "_workflow", lambda name: ci if name == "ci.yml" else workflow(name)
    )
    with pytest.raises(AssertionError, match="accidental-secret"):
        test_pr_jobs_with_credentials_require_a_successful_security_gate()


@pytest.mark.parametrize(
    ("job_name", "credential"),
    [
        ("deploy_prod", "    env:\n      TOKEN: ${{ secrets.CI_TOKEN }}\n"),
        ("Deploy", "    permissions:\n      id-token: write\n"),
        ("oidc-bypass", "    permissions:\n      id-token: write\n"),
        ("github-token-bypass", "    env:\n      GH_TOKEN: ${{ github.token }}\n"),
        ("write-permission-bypass", "    permissions:\n      contents: write\n"),
    ],
)
def test_credential_contract_rejects_write_or_token_bypasses(monkeypatch, job_name, credential):
    workflow = _workflow
    ci = workflow("ci.yml") + (
        f"\n  {job_name}:\n    runs-on: ubuntu-latest\n"
        "    needs: security-gate\n    if: ${{ always() }}\n" + credential
    )
    monkeypatch.setitem(
        globals(), "_workflow", lambda name: ci if name == "ci.yml" else workflow(name)
    )
    with pytest.raises(AssertionError, match=job_name):
        test_pr_jobs_with_credentials_require_a_successful_security_gate()


def test_gate_contract_rejects_an_audit_condition_that_bypasses_the_gate(monkeypatch):
    workflow = _workflow
    ci = workflow("ci.yml").replace(
        "if: ${{ !cancelled() && needs.security-gate.result == 'success' }}",
        "if: ${{ !cancelled() && needs.security-gate.result == 'success' || true }}",
        1,
    )
    monkeypatch.setitem(
        globals(), "_workflow", lambda name: ci if name == "ci.yml" else workflow(name)
    )
    with pytest.raises(AssertionError, match="unit-test-times"):
        test_pr_jobs_with_credentials_require_a_successful_security_gate()


def test_credential_contract_rejects_secrets_in_the_gate(monkeypatch):
    workflow = _workflow
    ci = workflow("ci.yml").replace(
        "    uses: ./.github/workflows/security-gate.yml\n",
        "    uses: ./.github/workflows/security-gate.yml\n    secrets: inherit\n",
        1,
    )
    monkeypatch.setitem(
        globals(), "_workflow", lambda name: ci if name == "ci.yml" else workflow(name)
    )
    with pytest.raises(AssertionError, match="security-gate"):
        test_pr_jobs_with_credentials_require_a_successful_security_gate()


def test_publisher_contract_rejects_a_new_ungated_job(monkeypatch):
    workflow = _workflow
    publish = (
        workflow("publish-skills-test.yml") + "\n  accidental-job:\n    runs-on: ubuntu-latest\n"
    )
    monkeypatch.setitem(
        globals(),
        "_workflow",
        lambda name: publish if name == "publish-skills-test.yml" else workflow(name),
    )
    with pytest.raises(AssertionError, match="accidental-job"):
        test_security_scan_provenance_and_gate()


def test_security_scan_provenance_and_gate():
    gate_job = (
        "  security-gate:\n    name: Security Gate\n    permissions:\n"
        "      actions: read\n    uses: ./.github/workflows/security-gate.yml\n"
    )
    ci = _workflow("ci.yml")
    publish = _workflow("publish-skills-test.yml")
    scan = _workflow("security-scan.yml")
    gate = _workflow("security-gate.yml")
    assert gate_job in ci and gate_job in publish
    jobs = dict(
        re.findall(
            r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)",
            publish.split("\njobs:\n", 1)[1],
        )
    )
    gated = {"security-gate"}
    while True:
        previous = gated.copy()
        for name, job in jobs.items():
            needs = re.search(r"(?m)^    needs: \[?([a-z0-9-, ]+)\]?$", job)
            header = job.split("\n    steps:", 1)[0]
            overrides_success = re.search(r"\b(?:always|failure|cancelled|success)\s*\(", header)
            if (
                overrides_success or "needs.security-gate.result" in header
            ) and not _requires_gate_success(header):
                continue
            if needs and {need.strip() for need in needs[1].split(",")} <= gated:
                gated.add(name)
        if gated == previous:
            break
    assert set(jobs) == gated, f"Publisher jobs without a successful gate: {set(jobs) - gated}"
    assert "workflow_run:\n    workflows: [CI]\n    types: [requested]" in scan
    assert "workflow_call:" not in scan
    assert "group: security-scan-${{ github.event.workflow_run.id }}" in scan
    assert "cancel-in-progress: true" in scan
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
        ([[]], False, 27),
        ([[{"status": "queued", "conclusion": None}]], False, 27),
        ([[{"created_at": "2026-11-12T00:00:00Z"}]], False, 27),
        ([[{"display_title": "Security Scan #8"}]], False, 27),
        ([[{"event": "pull_request"}]], False, 27),
        ([[{"event": "workflow_dispatch"}]], False, 27),
        ([[{"path": ".github/workflows/other.yml"}]], False, 27),
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
def test_security_gate_waits_for_the_current_scan(monkeypatch, responses, accepted, polls):
    code, calls = _run_security_gate(monkeypatch, responses)
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
    api_error=None,
    api_errors=None,
    observed=None,
    first_scan_page_matches=False,
):
    body = _workflow("security-gate.yml").split("        run: |\n", 1)[1].split("\n      - ", 1)[0]
    script = textwrap.dedent(body).removeprefix("python3 - <<'PY'\n").removesuffix("PY")
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
    fetches = []

    def pages(which, index):
        changes = responses() if callable(responses) else responses[min(index, len(responses) - 1)]
        runs = [request] if which == "request" else [scan | change for change in changes]
        first_page = (
            scan if which == "scan" and first_scan_page_matches else scan | {"path": "other.yml"}
        )
        return [
            {"workflow_runs": [first_page] * 100},
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

    def fetch(request, timeout):
        assert request.get_header("Authorization") == (
            "Bearer test-token" if authenticated else None
        )
        assert timeout == 30
        fetches.append(request.full_url)
        if api_error:
            raise api_error
        if api_errors and len(fetches) in api_errors:
            raise api_errors[len(fetches)]
        endpoint, page = request.full_url.rsplit("&page=", 1)
        assert endpoint.startswith("https://api.github.com/")
        which = endpoint_kind(endpoint.removeprefix("https://api.github.com/"))
        if page == "1":
            calls[which] += 1
        return io.StringIO(json.dumps(pages(which, calls[which] - 1)[int(page) - 1]))

    slept = []
    monkeypatch.setattr(urllib.request, "urlopen", fetch)
    monkeypatch.setattr("time.sleep", slept.append)
    monkeypatch.setattr("time.time", lambda: 1794602000 + sum(slept))
    with pytest.raises(SystemExit) as result:
        exec(compile(script, "security-gate.yml", "exec"), {})
    if not api_errors and not api_error:
        assert slept == ([10] * 12 + [30] * 6 + [60] + [120] * 7)[: len(slept)]
    assert sum(slept) <= 20 * 60
    if max(calls.values()) == 27:
        assert sum(slept) == 20 * 60
    assert len(fetches) <= 56
    if observed is not None:
        observed.update(sleeps=slept, fetches=fetches)
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
    assert code != 0 and calls == 27


@pytest.mark.parametrize("matches_commit", [True, False])
def test_security_gate_comment_requires_existing_scan_of_authorized_commit(
    monkeypatch, matches_commit
):
    # Comments arrive after the scan: reuse it without a PR-event freshness cutoff.
    sha = HEAD if matches_commit else "a" * 40
    code, calls = _run_security_gate(monkeypatch, [[{}]], updated="", sha=sha)
    assert (code == 0) == matches_commit
    assert calls == (1 if matches_commit else 27)


@pytest.mark.parametrize(("pr", "sha"), [("", HEAD), ("0", HEAD), ("7", ""), ("7", "main")])
def test_security_gate_rejects_missing_or_invalid_authorized_commit(monkeypatch, pr, sha):
    code, calls = _run_security_gate(monkeypatch, [[{}]], updated="", pr=pr, sha=sha)
    assert code != 0 and calls == 0


@pytest.mark.parametrize("authenticated", [True, False])
def test_security_gate_supports_anonymous_and_authenticated_requests(monkeypatch, authenticated):
    code, _ = _run_security_gate(monkeypatch, [[{}]], authenticated=authenticated)
    assert code == 0


@pytest.mark.parametrize(
    "error",
    [
        *[
            urllib.error.HTTPError("https://api.github.com", status, "failure", {}, None)
            for status in (403, 404)
        ],
        urllib.error.URLError("unreachable"),
        TimeoutError("timed out"),
    ],
)
def test_security_gate_api_errors_block_ci(monkeypatch, error):
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_error=error)
    assert str(code).startswith("::error::")
    assert "tests remain blocked" in code and "re-run failed CI jobs" in code


@pytest.mark.parametrize(
    "failure",
    [ConnectionResetError("body-secret"), http.client.IncompleteRead(b"body-secret", 1)],
)
def test_security_gate_fails_closed_when_forbidden_body_read_fails(monkeypatch, capsys, failure):
    class BrokenBody(io.BytesIO):
        def read(self, size=-1):
            raise failure

    error = urllib.error.HTTPError(
        "https://api.github.com", 403, "forbidden", {"X-Private": "header-secret"}, BrokenBody()
    )
    observed = {}
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_error=error, observed=observed)
    captured = capsys.readouterr()
    output = str(code) + captured.out + captured.err
    assert str(code).startswith("::error::")
    assert "tests remain blocked" in code
    assert not observed["sleeps"] and len(observed["fetches"]) == 1
    assert all(secret not in output for secret in ("test-token", "header-secret", "body-secret"))


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize("failure_call", [1, 3, 4])
def test_security_gate_waits_through_transient_server_error(monkeypatch, status, failure_call):
    error = urllib.error.HTTPError("https://api.github.com", status, "failure", {}, None)
    observed = {}
    code, _ = _run_security_gate(
        monkeypatch, [[{}]], api_errors={failure_call: error}, observed=observed
    )
    assert code == 0
    assert observed["sleeps"] == [10]


@pytest.mark.parametrize("status", [500, 503, 504])
def test_security_gate_persistent_server_errors_still_fail_closed(monkeypatch, status):
    error = urllib.error.HTTPError("https://api.github.com", status, "failure", {}, None)
    observed = {}
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_error=error, observed=observed)
    assert str(code).startswith("::error::")
    assert "did not complete" in code
    assert f"HTTP {status}" in code
    assert sum(observed["sleeps"]) == 20 * 60
    assert len(observed["fetches"]) == 27


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("failure_call", [1, 3, 4])
@pytest.mark.parametrize(("reset", "wait"), [(1794602003, 10), (1794602050, 51)])
def test_security_gate_waits_through_rate_limits(monkeypatch, status, failure_call, reset, wait):
    error = urllib.error.HTTPError(
        "https://api.github.com",
        status,
        "rate limited",
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
        None,
    )
    observed = {}
    code, _ = _run_security_gate(
        monkeypatch,
        [[{}]],
        authenticated=False,
        api_errors={failure_call: error},
        observed=observed,
    )
    assert code == 0
    assert observed["sleeps"] == [wait]


@pytest.mark.parametrize("reset", [None, "1794605000"])
def test_security_gate_waits_through_a_secondary_limit_without_retry_headers(monkeypatch, reset):
    headers = {"X-RateLimit-Remaining": "42"}
    if reset:
        headers["X-RateLimit-Reset"] = reset
    error = urllib.error.HTTPError(
        "https://api.github.com",
        403,
        "forbidden",
        headers,
        io.BytesIO(b'{"message":"You have exceeded a secondary rate limit."}'),
    )
    observed = {}
    code, _ = _run_security_gate(
        monkeypatch, [[{}]], authenticated=False, api_errors={1: error}, observed=observed
    )
    assert code == 0
    assert observed["sleeps"] == [60]


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize(("retry_after", "wait"), [(3, 10), (45, 45)])
def test_security_gate_honors_retry_after_without_a_reset_header(
    monkeypatch, status, retry_after, wait
):
    error = urllib.error.HTTPError(
        "https://api.github.com", status, "rate limited", {"Retry-After": str(retry_after)}, None
    )
    observed = {}
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_errors={1: error}, observed=observed)
    assert code == 0
    assert observed["sleeps"] == [wait]


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize(
    ("reset", "retry_after", "wait"),
    [(1794602003, 45, 45), (1794602050, 3, 10), (1794602050, 50, 50)],
)
def test_security_gate_waits_for_both_rate_limit_headers(
    monkeypatch, status, reset, retry_after, wait
):
    error = urllib.error.HTTPError(
        "https://api.github.com",
        status,
        "rate limited",
        {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset),
            "Retry-After": str(retry_after),
        },
        None,
    )
    observed = {}
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_errors={1: error}, observed=observed)
    assert code == 0
    assert observed["sleeps"] == [wait]


@pytest.mark.parametrize("status", [429, 403])
def test_security_gate_waits_at_least_one_minute_without_rate_limit_headers(monkeypatch, status):
    body = io.BytesIO(b'{"message":"You have exceeded a secondary rate limit."}')
    error = urllib.error.HTTPError("https://api.github.com", status, "rate limited", {}, body)
    observed = {}
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_errors={1: error}, observed=observed)
    assert code == 0
    assert observed["sleeps"] == [60]


def test_security_gate_ignores_primary_reset_when_secondary_retry_after_is_present(monkeypatch):
    error = urllib.error.HTTPError(
        "https://api.github.com",
        403,
        "forbidden",
        {"X-RateLimit-Remaining": "42", "X-RateLimit-Reset": "1794605000", "Retry-After": "60"},
        io.BytesIO(b'{"message":"You have exceeded a secondary rate limit."}'),
    )
    observed = {}
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_errors={1: error}, observed=observed)
    assert code == 0
    assert observed["sleeps"] == [60]


def test_security_gate_backs_off_repeated_secondary_limits_within_budget(monkeypatch):
    errors = {
        call: urllib.error.HTTPError("https://api.github.com", 429, "rate limited", {}, None)
        for call in range(1, 6)
    }
    observed = {}
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_errors=errors, observed=observed)
    assert "did not complete within 20 minutes" in code
    assert observed["sleeps"] == [60, 120, 240, 480, 300]
    assert len(observed["fetches"]) == 5


def test_security_gate_reserves_its_final_poll_after_repeated_rate_limits(monkeypatch):
    errors = {
        call: urllib.error.HTTPError(
            "https://api.github.com",
            429,
            "rate limited",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(1794602000 + call)},
            None,
        )
        for call in range(1, 27)
    }

    def responses():
        return [{}] if time.time() >= 1794602100 else [{"status": "in_progress"}]

    observed = {}
    code, _ = _run_security_gate(monkeypatch, responses, api_errors=errors, observed=observed)
    assert code == 0
    assert sum(observed["sleeps"]) == 20 * 60


def test_security_gate_does_not_trust_a_partial_rate_limited_scan_listing(monkeypatch):
    error = urllib.error.HTTPError(
        "https://api.github.com", 429, "rate limited", {"X-RateLimit-Reset": "1794602003"}, None
    )
    code, _ = _run_security_gate(
        monkeypatch,
        [[{"id": 2, "conclusion": "failure"}]],
        api_errors={4: error},
        first_scan_page_matches=True,
    )
    assert code == 1


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("reset", [1794602003, 1794604000])
def test_security_gate_rate_limit_persists_until_the_existing_deadline(monkeypatch, status, reset):
    error = urllib.error.HTTPError(
        "https://api.github.com",
        status,
        "rate limited",
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
        None,
    )
    observed = {}
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_error=error, observed=observed)
    assert f"HTTP {status}" in code
    assert str(reset) in code
    if reset == 1794604000:
        assert "outlasts the 20-minute wait" in code
        assert not observed["sleeps"]
        assert len(observed["fetches"]) == 1
    else:
        assert "did not complete within 20 minutes" in code
        assert sum(observed["sleeps"]) == 20 * 60
        assert len(observed["fetches"]) == 27


@pytest.mark.parametrize("status", [403, 429])
def test_security_gate_fails_immediately_when_retry_after_exceeds_budget(monkeypatch, status):
    error = urllib.error.HTTPError(
        "https://api.github.com", status, "rate limited", {"Retry-After": "1201"}, None
    )
    observed = {}
    code, _ = _run_security_gate(monkeypatch, [[{}]], api_error=error, observed=observed)
    assert "outlasts the 20-minute wait" in code
    assert f"HTTP {status}" in code and "1794603201" in code
    assert not observed["sleeps"] and len(observed["fetches"]) == 1


def test_security_gate_supports_required_workflow_events():
    workflow = _workflow("security-gate.yml")
    trigger = workflow.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
    assert "  workflow_call:\n" in trigger
    assert "  pull_request:\n  merge_group:\n" in trigger
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
    wait_step = workflow.split("      - name: Wait for the shared Security Scan\n", 1)[1]
    condition = re.search(r"        if: (.*)", wait_step)[1]
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
    assert re.search(
        r"(?m)^on:\n  pull_request:\n    types: \[labeled, ready_for_review\]\n", workflow
    )
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
        "codex-relay",
        "antigravity",
        "managed",
        "codex-desktop-macos",
        "codex-desktop-windows",
        "dedicated-cuj-plan",
        "dedicated-cuj",
        "live-shard-times",
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
            "codex-relay",
            "antigravity",
            "managed",
            "codex-desktop-macos",
            "codex-desktop-windows",
            "dedicated-cuj-plan",
            "dedicated-cuj",
            "live-shard-times",
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
                        len(
                            marks
                            & {"claude", "codex", "opencode", "copilot", "antigravity", "vscode"}
                        )
                        == 1
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


@pytest.mark.skipif(sys.platform == "win32", reason="CI parses these on Linux runners")
def test_cowork_shell_scripts_parse():
    # The ${VAR:?message} word re-parses quotes, so an apostrophe in it breaks the whole script;
    # bash -n keeps that from reaching a macOS lane.
    scripts = sorted((Path(__file__).parent.parent / "scripts/cowork-inference").rglob("*.sh"))
    assert scripts
    for path in scripts:
        result = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"{path}: {result.stderr}"


@pytest.mark.parametrize("job_name", ["cowork-inference-windows", "cowork-inference-macos"])
def test_cowork_ci_permissions_and_required_gate(job_name):
    jobs = _integration_jobs()
    job = jobs[job_name]
    assert "github.event_name != 'merge_group'" in job
    condition = re.search(r"(?m)^    if: (.*)$", job)
    assert condition is not None
    assert condition.group(1) == (
        "${{ inputs.mode != 'advisory' && github.event_name != 'merge_group' && "
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


@pytest.mark.parametrize("job_name", ["cowork-inference-windows", "cowork-inference-macos"])
def test_cowork_ci_covers_each_claude_model_source(job_name):
    job = _integration_jobs()[job_name]
    assert "      fail-fast: false\n" in job
    hosted = (
        "            provider: ''\n            model: system.ai.claude-haiku-4-5\n"
        "            relay: false\n"
    )
    relay = (
        "            provider: main.ucode.ci_e2e_anthropic_relay_mps\n"
        "            model: claude-haiku-4-5-20251001\n            relay: true\n"
    )
    legacy_anthropic = (
        "            provider: main.ucode.ci_e2e_anthropic_nonrelay_mps\n"
        "            model: claude-haiku-4-5-20251001\n            relay: false\n"
    )
    assert hosted in job
    assert relay in job
    # Windows runs the non-relay service in the staging anthropic-mps lane instead.
    assert (legacy_anthropic in job) is job_name.endswith("macos")
    for variable in ("PROVIDER", "MODEL", "RELAY"):
        assert f"      UG_COWORK_{variable}: ${{{{ matrix.source.{variable.lower()} }}}}\n" in job
    # Only the relay source receives the subscription token.
    tokens = re.findall(r"(?m)^\s+CLAUDE_CODE_OAUTH_TOKEN: (.*)$", job)
    assert tokens
    assert set(tokens) == {"${{ matrix.source.relay && secrets.CLAUDE_CODE_OAUTH_TOKEN || '' }}"}
    artifact = "cowork-inference" if job_name.endswith("windows") else "cowork-inference-macos"
    assert re.findall(r"(?m)^          name: (cowork-inference.*)$", job) == [
        f"{artifact}-${{{{ matrix.source.id }}}}"
    ]


@pytest.mark.parametrize("job_name", ["cowork-inference-windows", "cowork-inference-macos"])
def test_cowork_ci_runs_mcp_web_search_after_inference(job_name):
    job = _integration_jobs()[job_name]
    step = job.split("      - name: Assert Cowork MCP web search\n", 1)[1].split(
        "      - name: ", 1
    )[0]
    assert (
        "        if: ${{ !cancelled() && steps.cowork-inference.outcome == 'success' }}\n" in step
    )
    # `ug mcp add` and the connector proxy authenticate against the e2e workspace.
    assert "UCODE_TEST_WORKSPACE: ${{ secrets.UCODE_TEST_WORKSPACE }}" in step
    assert "DATABRICKS_BEARER: ${{ secrets.DATABRICKS_BEARER }}" in step
    assert ("-Capability mcp" in step) or ("--capability mcp" in step)
    # The PAT minted for Desktop's connectors is revoked even when the job fails.
    revoke = job.split("      - name: Revoke the Desktop PAT\n", 1)[1].split("      - name: ", 1)[0]
    assert "        if: always()\n" in revoke
    assert "desktop-pat.py revoke" in revoke
    assert "DATABRICKS_BEARER: ${{ secrets.DATABRICKS_BEARER }}" in revoke
    # configure selects the MCP service for Desktop's connectors.
    assert "      UG_COWORK_MCP: true\n" in job


def test_staging_anthropic_mps_lane_runs_cowork_only_on_windows_claude():
    jobs = _integration_jobs()
    job = jobs["anthropic-mps"]
    assert (
        "      UG_COWORK_DESKTOP: ${{ github.event_name != 'merge_group' && "
        "matrix.os == 'windows-latest' && matrix.agent == 'claude' && matrix.shard_index == 1 }}\n"
    ) in job
    assert "      UG_COWORK_PROVIDER: main.ucode.dont_delete_ci_company_anthropic_mps\n" in job
    assert "      UG_COWORK_MODEL: claude-haiku-4-5-20251001\n" in job
    assert "      UG_COWORK_RELAY: false\n" in job
    # The shared live steps reach every live job; only this lane opts in.
    opted_in = [name for name, body in jobs.items() if "\n      UG_COWORK_DESKTOP:" in body]
    assert opted_in == ["anthropic-mps"]
    workflow = (Path(__file__).parent.parent / ".github/workflows/integration.yml").read_text()
    live_steps = workflow.split("    steps: &live-steps\n", 1)[1].split("\n\n", 1)[0]
    desktop = live_steps.split("      - name: Install Claude Desktop\n", 1)[1]
    gates = re.findall(r"(?m)^        if: (.*)$", desktop)
    # Every Desktop step is skipped unless the lane opts in through the install step.
    assert gates[0] == "${{ !cancelled() && env.UG_COWORK_DESKTOP == 'true' }}"
    assert all("steps.desktop-" in gate for gate in gates[1:])
    assert len(gates) == desktop.count("      - name: ") + 1
    assert "DATABRICKS_BEARER_COMMAND=" in desktop
    assert "name: cowork-inference-anthropic\n" in desktop
    # The staging lane runs the same MCP web search; its PAT is minted with the bearer command.
    assert "      UG_COWORK_MCP: true\n" in job
    mcp = desktop.split("      - name: Assert Cowork MCP web search\n", 1)[1].split(
        "      - name: ", 1
    )[0]
    assert (
        "        if: ${{ !cancelled() && steps.desktop-inference.outcome == 'success' }}\n" in mcp
    )
    assert "-Capability mcp" in mcp
    revoke = desktop.split("      - name: Revoke the Desktop PAT\n", 1)[1].split(
        "      - name: ", 1
    )[0]
    assert (
        "        if: ${{ always() && steps.desktop-dependencies.outcome == 'success' }}\n" in revoke
    )
    assert "desktop-pat.py revoke" in revoke
    assert "${{ runner.temp }}/cowork-inference/mcp/\n" in desktop
    opted_in = [name for name, body in jobs.items() if "\n      UG_COWORK_MCP:" in body]
    assert sorted(opted_in) == [
        "anthropic-mps",
        "cowork-inference-macos",
        "cowork-inference-windows",
    ]


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
@pytest.mark.parametrize("gate_result", ["success", "failure", "skipped", "cancelled"])
def test_required_e2e_enforces_integration_result(event, integration_result, passes, gate_result):
    required = _workflow("ci.yml").split("\n  required-e2e:\n", 1)[1]
    script = required.split("        run: ", 1)[1].split("\n      - ", 1)[0].strip()
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            **os.environ,
            "EVENT_NAME": event,
            "GATE_RESULT": gate_result,
            "RESULT": "success",
            "INTEGRATION_RESULT": integration_result,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is (passes and gate_result == "success"), result.stderr


def test_vscode_lane_pins_vscode_and_copilot_and_is_non_blocking():
    workflow = WORKFLOW.read_text()
    jobs = _integration_jobs()
    job = jobs["vscode"]
    assert "  VSCODE_VERSION: ${{ inputs.vscode_version || '1.147.0' }}" in workflow
    assert (
        'vscode) args=(--copilot-version "$COPILOT_VERSION" --vscode-version "$VSCODE_VERSION") ;;'
        in workflow
    )
    assert "runs-on: ${{ matrix.os }}" in job
    assert "os: [ubuntu-22.04, windows-latest]" in job
    assert "continue-on-error: true" in job
    assert "AGENT: vscode" in job
    assert "TEST_MARKER: live and vscode" in job
    assert (
        "      - name: Install a virtual display for VS Code\n"
        "        if: ${{ env.AGENT == 'vscode' && runner.os == 'Linux' }}\n"
        "        run: sudo apt-get update && sudo apt-get install -y --no-install-recommends xvfb"
    ) in workflow
    gate_needs = re.search(r"(?m)^    needs: \[(.*)\]$", jobs["cujs"]).group(1)
    assert "vscode" not in [need.strip() for need in gate_needs.split(",")]


def test_latest_harness_drives_the_newest_vscode_and_copilot():
    workflow = _workflow("harness-latest.yml")
    resolver, harness = workflow.split("\n  harness-latest:\n", 1)
    assert "npm view @github/copilot@latest version" in resolver
    assert "https://update.code.visualstudio.com/api/update/linux-x64/stable/latest" in resolver
    for output in ("copilot", "vscode", "vscode_sha256"):
        assert f"needs.resolve-versions.outputs.{output}" in harness
    assert "- suite: vscode\n            runner: ubuntu-22.04\n" in harness
    assert "marker: harness_latest and vscode" in harness
    assert (
        'args=(--copilot-version "$COPILOT_VERSION" --vscode-version "$VSCODE_VERSION"\n'
        '              --vscode-sha256 "$VSCODE_SHA256")'
    ) in harness
    assert "if: ${{ matrix.suite == 'vscode' }}" in harness


@pytest.mark.skipif(sys.platform == "win32", reason="required-tests runs on Linux")
@pytest.mark.parametrize(
    "failing",
    [
        None,
        "GATE_RESULT",
        "LINT_RESULT",
        "RESULT",
        "WINDOWS_RESULT",
        "PROVISIONING_RESULT",
        "VSCODE_EXTENSION_RESULT",
    ],
)
@pytest.mark.parametrize("bad", ["failure", "skipped", "cancelled"])
def test_required_tests_enforces_lint_result(failing, bad):
    required = (
        _workflow("ci.yml").split("\n  required-tests:\n", 1)[1].split("\n  required-e2e:\n")[0]
    )
    assert (
        "    needs: [security-gate, checks, unit-linux, unit-windows, unit-test-times, "
        "windows-provisioning, vscode-extension]\n" in required
    )
    script = required.split("        run: |", 1)[1].split("\n      - ", 1)[0]
    names = [
        "GATE_RESULT",
        "LINT_RESULT",
        "RESULT",
        "WINDOWS_RESULT",
        "PROVISIONING_RESULT",
        "VSCODE_EXTENSION_RESULT",
    ]
    env = {
        **os.environ,
        **dict.fromkeys(names, "success"),
        "COVERAGE_RESULT": "success",
    }
    if failing:
        env[failing] = bad
    result = subprocess.run(
        ["/bin/bash", "-c", textwrap.dedent(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is (failing is None), result.stderr


def test_integration_requires_a_passing_security_gate():
    # `!cancelled()` alone would let integration receive secrets after a failed scan.
    integration = (
        _workflow("ci.yml").split("\n  integration:\n", 1)[1].split("\n  required-tests:\n")[0]
    )
    condition = re.search(r"(?m)^    if: (.*)$", integration).group(1)
    assert "needs.security-gate.result == 'success'" in condition
    assert "security-gate" in re.search(r"(?m)^    needs: (.*)$", integration).group(1)


@pytest.mark.parametrize(
    ("args", "workspace", "workers"),
    [
        # The unit CI job passes only --ignore=tests/test_e2e.py, which pytest keeps out of args.
        ([], "", None),
        (["tests/test_cli.py"], "", None),
        (["tests/test_e2e.py"], "", 0),
        (["tests/test_e2e.py::TestUnityGateway"], "", 0),
        ([], "https://workspace.example", 0),
    ],
)
def test_live_runs_stay_serial_under_xdist(monkeypatch, args, workspace, workers):
    from types import SimpleNamespace

    from tests import conftest

    monkeypatch.setenv("UCODE_TEST_WORKSPACE", workspace)
    assert conftest.pytest_xdist_auto_num_workers(SimpleNamespace(args=args)) == workers


@pytest.mark.parametrize(
    ("args", "parallel"),
    [
        # Deselect every live test: the hook decides the worker count before collection.
        (["tests/test_e2e.py", "-k", "no_such_test"], False),
        (["tests/test_jsonc.py"], True),
    ],
)
def test_pytest_discovers_the_serial_live_hook(args, parallel):
    # The child run must not inherit this CI shard or overwrite its coverage report.
    skip = {"UCODE_TEST_WORKSPACE", "UG_TEST_SHARD", "UG_TEST_SHARD_REPORT", "PYTEST_ADDOPTS"}
    # Two workers: enough to show the hook left unit runs parallel without starting one child
    # worker per CPU inside every parent xdist worker.
    env = {key: value for key, value in os.environ.items() if key not in skip}
    env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = "2"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=Path(__file__).parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode in (0, 5), result.stdout + result.stderr
    assert ("workers [" in result.stdout) is parallel, result.stdout


def test_unit_shards_run_every_test_exactly_once():
    from tests import shard

    files = [f"tests/test_{name}.py" for name in "abcdefgh"]
    slow = {"tests/test_a.py::test_0": 9.0, "tests/test_a.py::test_1": 3.0}
    times = {"files": {"tests/test_a.py": [40.0, 100], "tests/test_b.py": [0.5, 10]}, "slow": slow}
    nodeids = [
        f"{path}::test_{index}" for path in files for index in range(100 if "_a" in path else 10)
    ]
    for total in (1, 3, 20, 40):
        plan = shard.plan(files, total, times)
        assert plan == shard.plan(list(files), total, times)  # identical on every shard
        owners = [
            [
                index
                for index, owned in enumerate(shard.owned_tests(units, nodeids, set(slow)))
                if owned
            ]
            for units in plan
        ]
        assert sorted(index for owned in owners for index in owned) == list(range(len(nodeids)))
    # Slow tests are units of their own; the rest of a slow file spreads across shards.
    plan = shard.plan(files, 8, times)
    assert sum(("test", "tests/test_a.py::test_0") in units for units in plan) == 1
    owners = {
        index for index, units in enumerate(plan) for unit in units if unit[1] == "tests/test_a.py"
    }
    # 40 s less 12 s of slow tests leaves 28 s: seven 4-second chunks on seven shards.
    assert len(owners) == 7
    assert shard.test_files() == sorted(
        f"tests/{path.name}"
        for path in Path(shard.TESTS).glob("test_*.py")
        if path.name != "test_e2e.py"
    )


def test_unit_shard_loads_stay_balanced():
    from tests import shard

    for system, total in (("Linux", 24), ("Windows", 64)):
        times = shard.load_times(system)
        assert all(seconds >= shard.SLOW_SECONDS for seconds in times["slow"].values())
        plan = shard.plan(shard.test_files(), total, times)
        units = sorted(unit for owned in plan for unit in owned)
        assert len(units) == len(set(units))
        weights = []
        for owned in plan:
            load = 0.0
            for unit in owned:
                if unit[0] == "test":
                    load += times["slow"][unit[1]]
                else:
                    slow = sum(v for k, v in times["slow"].items() if shard.file_of(k) == unit[1])
                    load += max(times["files"].get(unit[1], [1.0])[0] - slow, 0.0) / unit[3]
            weights.append(load)
        assert max(weights) - min(weights) <= max(times["slow"].values()), weights


@pytest.mark.parametrize("spec", ["0/4", "5/4", "x/4"])
def test_unit_shard_spec_rejects_bad_values(spec):
    from tests import shard

    with pytest.raises(ValueError):
        shard.parse(spec)


def _write_unit_shard_artifact(root, system, *, first_file_cases, first_file_collected):
    """Write one downloaded shard artifact that owns every unit file whole.

    The report claims the first file collected ``first_file_collected`` tests while its JUnit
    holds exactly ``first_file_cases``, so a defect under test can make the two disagree.
    """
    from tests import shard

    files = shard.test_files()
    directory = root / f"unit-shard-{system}-1"
    directory.mkdir(parents=True)
    collected = {}
    cases = []
    for path in files:
        module = Path(path).stem
        if path == files[0]:
            file_cases, count = first_file_cases, first_file_collected
        else:
            file_cases = [
                (f"tests.{module}", "test_0", 0.1),
                (f"tests.{module}", "test_1", 0.1),
            ]
            count = 2
        cases += file_cases
        collected[path] = count
    junit = (
        "<testsuite>"
        + "".join(
            f'<testcase classname="{classname}" name="{name}" time="{seconds}"/>'
            for classname, name, seconds in cases
        )
        + "</testsuite>"
    )
    (directory / "junit.xml").write_text(junit, encoding="utf-8")
    (directory / "shard-report.json").write_text(
        json.dumps(
            {
                "shard": "1/1",
                "units": [["chunk", path, 1, 1] for path in files],
                "collected": collected,
                "selected": sum(collected.values()),
            }
        ),
        encoding="utf-8",
    )


def _audit_unit_shards(tmp_path):
    """Run scripts/unit_test_times.py over tmp_path's shard artifacts; return the result."""
    script = Path(__file__).parent.parent / "scripts" / "unit_test_times.py"
    output = tmp_path / "unit_test_times.json"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "shards"), str(output)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, output


def test_unit_test_times_accepts_a_complete_shard_run(tmp_path):
    """A both-OS shard set covering every test once passes and rebuilds class-qualified ids."""
    from tests import shard

    files = shard.test_files()
    first = Path(files[0])
    slow = round(shard.SLOW_SECONDS + 1, 2)
    # Every file collects two tests; the first file also holds a class-based slow test, to
    # prove the audit rebuilds its full nodeid from the JUnit classname.
    cases = [
        (f"tests.{first.stem}", "test_0", 0.1),
        (f"tests.{first.stem}", "test_1", 0.1),
        (f"tests.{first.stem}.TestRebuild", "test_slow", slow),
    ]
    for system in ("Linux", "Windows"):
        _write_unit_shard_artifact(
            tmp_path / "shards", system, first_file_cases=cases, first_file_collected=3
        )
    result, output = _audit_unit_shards(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    times = json.loads(output.read_text(encoding="utf-8"))
    assert sorted(times) == ["Linux", "Windows"]
    for system in ("Linux", "Windows"):
        assert sorted(times[system]["files"]) == files
        assert times[system]["slow"][f"{files[0]}::TestRebuild::test_slow"] == slow


@pytest.mark.parametrize(
    "defect",
    ["a test ran twice", "a test never ran", "an OS never reported"],
)
def test_unit_test_times_rejects_incomplete_shard_runs(defect, tmp_path):
    """A re-run test, a lost test or a missing OS fails the exactly-once audit.

    A test that ran twice while another in the same file never ran keeps every per-file count
    correct, so only the per-test check catches it.
    """
    from tests import shard

    files = shard.test_files()
    first = Path(files[0])
    if defect == "a test ran twice":
        cases = [
            (f"tests.{first.stem}", "test_0", 0.1),
            (f"tests.{first.stem}", "test_0", 0.1),
        ]
        collected = 2
    else:
        cases = [(f"tests.{first.stem}", "test_0", 0.1)]
        collected = 2
    _write_unit_shard_artifact(
        tmp_path / "shards", "Linux", first_file_cases=cases, first_file_collected=collected
    )
    if defect != "an OS never reported":
        _write_unit_shard_artifact(
            tmp_path / "shards",
            "Windows",
            first_file_cases=cases,
            first_file_collected=collected,
        )
    result, _ = _audit_unit_shards(tmp_path)
    # SystemExit reports its message on stderr, where CI's job log shows it.
    assert result.returncode != 0, result.stdout + result.stderr
    assert "::error::Unit shards did not cover the suite" in result.stderr
    if defect == "a test ran twice":
        assert f"Linux: {files[0]}::test_0 ran 2 times" in result.stderr
    elif defect == "a test never ran":
        assert f"Linux: {files[0]} ran 1 of 2 tests" in result.stderr
    else:
        assert "Windows: no unit shard artifacts" in result.stderr


LIVE_SHARD_LANES = (
    # (job, lane, agent) for the integration lanes CI splits by test file and case.
    ("full", "full-claude", "claude"),
    ("anthropic-mps", "anthropic-mps-claude", "claude"),
)


@pytest.mark.parametrize(("job_name", "lane", "agent"), LIVE_SHARD_LANES)
def test_live_shards_run_every_case_exactly_once(job_name, lane, agent):
    from tests import live_shard, shard

    jobs = _integration_jobs()
    times = live_shard.lane_times(lane, "Windows")
    cases = sorted(_collect_integration_cases(_ci_marker_expression(jobs[job_name], agent)))
    assert cases
    files = sorted({shard.file_of(case) for case in cases})
    owners = []
    for index in range(1, times["shards"] + 1):
        units = live_shard.units_for(lane, f"{index}/{times['shards']}", files, "Windows")
        owned = shard.owned_tests(units, cases, set(times["slow"]))
        owners.append([case for case, mine in zip(cases, owned, strict=True) if mine])
    # Every case of the lane runs in exactly one shard, whatever files CI collects on the lane.
    assert sorted(case for owned in owners for case in owned) == cases


@pytest.mark.parametrize("lane", [entry[1] for entry in LIVE_SHARD_LANES])
def test_live_shard_loads_stay_balanced(lane):
    from tests import live_shard, shard

    times = live_shard.lane_times(lane, "Windows")
    assert times["files"], "record fresh times from the live-test-times artifact"
    files = sorted(times["files"])
    total = times["shards"]
    plan = [
        live_shard.units_for(lane, f"{index}/{total}", files, "Windows")
        for index in range(1, total + 1)
    ]
    units = sorted(unit for owned in plan for unit in owned)
    assert len(units) == len(set(units))
    weights = []
    for owned in plan:
        load = 0.0
        for unit in owned:
            if unit[0] == "test":
                load += times["slow"][unit[1]]
            else:
                slow = sum(v for k, v in times["slow"].items() if shard.file_of(k) == unit[1])
                load += max(times["files"].get(unit[1], [1.0])[0] - slow, 0.0) / unit[3]
        weights.append(load)
    assert max(weights) - min(weights) <= max(times["slow"].values()), weights


@pytest.mark.parametrize(("lane", "job_name"), [(entry[1], entry[0]) for entry in LIVE_SHARD_LANES])
def test_live_shard_matrix_matches_the_recorded_shard_count(lane, job_name):
    from tests import live_shard

    jobs = _integration_jobs()
    total = live_shard.lane_times(lane, "Windows")["shards"]
    legs = re.findall(r'(?m)^            shard: "(\d+)/(\d+)"$', jobs[job_name])
    if legs:  # full: the shard legs are plain matrix entries.
        assert sorted(legs, key=lambda leg: int(leg[0])) == [
            (str(index), str(total)) for index in range(1, total + 1)
        ]
    else:  # anthropic-mps: the mode input selects the legs, so the shards live in its JSON.
        for index in range(1, total + 1):
            assert f'"shard":"{index}/{total}"' in jobs[job_name]
        assert "inputs.mode == 'advisory' &&" in jobs[job_name]
    # Sharded lanes feed the spec to the shared runner step; every leg uploads its own artifact.
    assert "LIVE_SHARD: ${{ matrix.shard || '' }}" in jobs[job_name]
    assert f"LIVE_SHARD_LANE: {lane}" in jobs[job_name]
    assert "${{ matrix.shard && '-shard' || '' }}${{ matrix.shard_index || '' }}" in jobs[job_name]


def test_cuj_shards_run_every_case_exactly_once():
    from tests import live_shard, shard

    test = "test_cuj4_smart_routing.py"
    lane = f"cuj-{test}"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--confcutdir=tests/e2e_cuj",
            f"tests/e2e_cuj/{test}",
            "--collect-only",
            "-q",
        ],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cases = sorted(line for line in result.stdout.splitlines() if line.startswith("test_"))
    assert cases
    times = live_shard.lane_times(lane, "Linux")
    owners = []
    for index in range(1, times["shards"] + 1):
        units = live_shard.units_for(lane, f"{index}/{times['shards']}", [test], "Linux")
        owned = shard.owned_tests(units, cases, set(times["slow"]))
        owners.append([case for case, mine in zip(cases, owned, strict=True) if mine])
    assert sorted(case for owned in owners for case in owned) == cases


@pytest.mark.skipif(sys.platform == "win32", reason="the CUJ lanes run on Linux")
def test_cuj_shards_deselect_through_a_real_pytest_run(tmp_path):
    # Drive the real collection hooks: each shard keeps only its cases, writes its report, and
    # the shards together keep every case exactly once.
    skip = {"UCODE_TEST_WORKSPACE", "UG_TEST_SHARD", "UG_TEST_SHARD_REPORT", "PYTEST_ADDOPTS"}
    shards = []
    for index in (1, 2, 3):
        report = tmp_path / f"shard-{index}.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "--confcutdir=tests/e2e_cuj",
                "tests/e2e_cuj/test_cuj4_smart_routing.py",
                "--collect-only",
                "-q",
            ],
            cwd=Path(__file__).parent.parent,
            env={
                **{key: value for key, value in os.environ.items() if key not in skip},
                "UG_LIVE_SHARD": f"{index}/3",
                "UG_LIVE_SHARD_LANE": "cuj-test_cuj4_smart_routing.py",
                "UG_LIVE_SHARD_REPORT": str(report),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        kept = {line for line in result.stdout.splitlines() if line.startswith("test_")}
        assert kept
        written = json.loads(report.read_text())
        assert written["shard"] == f"{index}/3"
        assert written["expected"]["test_cuj4_smart_routing.py"] == len(kept)
        shards.append(kept)
    assert len(set().union(*shards)) == sum(len(kept) for kept in shards)
    baseline = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--confcutdir=tests/e2e_cuj",
            "tests/e2e_cuj/test_cuj4_smart_routing.py",
            "--collect-only",
            "-q",
        ],
        cwd=Path(__file__).parent.parent,
        env={key: value for key, value in os.environ.items() if key not in skip},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert set().union(*shards) == {
        line for line in baseline.stdout.splitlines() if line.startswith("test_")
    }


def test_run_integration_accepts_both_shard_flags():
    # The flags left the mutually exclusive group once (argparse: "not allowed with argument"),
    # which failed every Full journeys - Claude - Windows shard in 28 s.
    spec = importlib.util.spec_from_file_location(
        "run_integration", Path(__file__).parent.parent / "scripts/run_integration.py"
    )
    run_integration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_integration)
    base = [
        "--claude-version",
        "2.1.290",
        "--python",
        "3.12",
        "--ug-version",
        "checkout",
        "--workspace",
        "https://example.databricks.com",
        "--profile",
        "ci",
    ]
    args = run_integration.arguments(base + ["--test-shard", "1/4", "--shard-lane", "full-claude"])
    assert args.test_shard == "1/4" and args.shard_lane == "full-claude"
    with pytest.raises(SystemExit):
        run_integration.arguments(base + ["--test-shard", "1/4"])


def test_live_shard_coverage_gate_downloads_every_shard_artifact():
    job = _workflow_job("live-shard-times", "cujs")
    for pattern in (
        "integration-full-claude-windows-latest-shard*",
        "integration-anthropic-mps-claude-windows-latest-shard*",
        "e2e-cujs-*shard*",
    ):
        assert f"pattern: {pattern}" in job
    assert "python3 scripts/live_shard_times.py" in job
    gate = _workflow_job("cujs", "codex-desktop-macos")
    assert "live-shard-times" in re.search(r"(?m)^    needs: \[([^\]]+)\]$", gate).group(1)


JUNIT_CASE = (
    '<?xml version="1.0" encoding="utf-8"?><testsuite name="x" tests="{count}">{cases}</testsuite>'
)


def _fake_shard(root, name, report, cases):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "shard-report.json").write_text(json.dumps(report), encoding="utf-8")
    body = "".join(
        f'<testcase classname="{classname}" name="{case}" time="{seconds}"/>'
        for classname, case, seconds in cases
    )
    (directory / "junit.xml").write_text(JUNIT_CASE.format(count=len(cases), cases=body))


def _fake_lane_artifacts(root, lane, system, total):
    """One case per shard: disjoint units, agreeing collected files, no marker drops."""
    for index in range(1, total + 1):
        _fake_shard(
            root,
            f"{lane}-{system}-shard{index}",
            {
                "lane": lane,
                "os": system,
                "shard": f"{index}/{total}",
                "units": [["test", f"test_x.py::c{index}"]],
                "files": {"test_x.py": total},
                "expected": {"test_x.py": 1},
                "marker_deselected": {},
            },
            [("test_x", f"c{index}", "3.0")],
        )


def test_live_shard_times_verifies_coverage_and_records(tmp_path):
    from tests import live_shard

    lanes = live_shard.expected_shards()
    for lane, (system, total) in sorted(lanes.items()):
        _fake_lane_artifacts(tmp_path, lane, system, total)
    recorded = tmp_path / "recorded.json"
    result = subprocess.run(
        [sys.executable, "scripts/live_shard_times.py", str(tmp_path), str(recorded)],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads(recorded.read_text())
    assert set(output) == set(lanes)
    total = lanes["full-claude"][1]
    assert output["full-claude"] == {
        "Windows": {
            "shards": total,
            "files": {"test_x.py": [round(3.0 * total, 2), total]},
            "slow": {f"test_x.py::c{index}": 3.0 for index in range(1, total + 1)},
        }
    }
    # A shard that never uploaded its artifact fails the proof for its whole lane.
    shutil.rmtree(tmp_path / f"full-claude-Windows-shard{total}")
    result = subprocess.run(
        [sys.executable, "scripts/live_shard_times.py", str(tmp_path), str(recorded)],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "full-claude/Windows: shard set is" in result.stderr + result.stdout


def test_checks_run_exactly_the_just_lint_commands():
    """`Checks` runs ruff and ty without just; the commands must not drift from `just lint`."""
    justfile = (Path(__file__).parent.parent / "justfile").read_text()
    checks = _workflow("ci.yml").split("\n  checks:\n", 1)[1].split("\n  unit-linux:\n", 1)[0]
    for recipe, command in (
        ("ruff-check", "ruff check src/ tests/"),
        ("ruff-format-check", "ruff format --check src/ tests/"),
        ("ty", "ty check src/"),
    ):
        assert f"{recipe}:\n    uv run {command}\n" in justfile, recipe
        assert f'"$VENV_BIN/{command.split()[0]}" {command.split(" ", 1)[1]}' in checks, recipe
    assert "lint: ruff-check ruff-format-check ty\n" in justfile


@pytest.mark.skipif(sys.platform == "win32", reason="required-tests runs on Linux")
@pytest.mark.parametrize("coverage", ["success", "skipped", "failure"])
def test_required_tests_requires_unit_shard_coverage(coverage):
    required = (
        _workflow("ci.yml").split("\n  required-tests:\n", 1)[1].split("\n  required-e2e:\n")[0]
    )
    script = required.split("        run: |", 1)[1].split("\n      - ", 1)[0]
    names = [
        "GATE_RESULT",
        "LINT_RESULT",
        "RESULT",
        "WINDOWS_RESULT",
        "PROVISIONING_RESULT",
        "VSCODE_EXTENSION_RESULT",
    ]
    env = {
        **os.environ,
        **dict.fromkeys(names, "success"),
        "COVERAGE_RESULT": coverage,
    }
    result = subprocess.run(
        ["/bin/bash", "-c", textwrap.dedent(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is (coverage == "success"), result.stderr


def test_desktop_chain_is_serial_and_basic_lanes_are_independent():
    jobs = _integration_jobs()
    assert not re.search(r"(?m)^    needs:", jobs["codex-desktop-macos"])
    assert "\n    needs: codex-desktop-macos\n" in jobs["codex-desktop-windows"]
    for name in ("codex-desktop-macos", "codex-desktop-windows", "managed-smart"):
        assert "    if: ${{ !cancelled() && " in jobs[name]
    assert "      max-parallel: 1\n" not in jobs["managed"]


def test_ci_splits_blocking_and_advisory_integration_after_security_gate():
    workflow = _workflow("ci.yml")
    jobs = dict(re.findall(r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)", workflow))
    required = jobs["integration"]
    advisory = jobs["integration-advisory"]
    for job in (required, advisory):
        assert "needs.security-gate.result == 'success'" in job
        assert "github.event_name != 'merge_group'" in job
        assert "uses: ./.github/workflows/integration.yml" in job
    assert "needs: security-gate" in required
    assert "mode: required" in required
    assert "needs: [security-gate, integration]" in advisory
    assert "mode: advisory" in advisory
    assert "needs: [security-gate, e2e, integration]" in jobs["required-e2e"]


def test_integration_modes_explain_jobs_run_by_the_other_caller():
    jobs = _integration_jobs()
    for job_name, job in jobs.items():
        condition = re.search(r"(?m)^    if: (.*)$", job)
        condition = condition.group(1) if condition else ""
        if "inputs.mode != 'advisory'" in condition or job_name == "live-shard-times":
            mode, other = "advisory", "Integration"
        elif "inputs.mode != 'required'" in condition:
            mode, other = "required", "Integration advisory"
        else:
            continue
        name = re.search(r"(?m)^    name: (.*)$", job).group(1)
        prefix = f"${{{{ inputs.mode == '{mode}' && '"
        assert name.startswith(prefix), (job_name, name)
        assert f"(runs in {other})' ||" in name, (job_name, name)


def test_integration_mode_union_covers_the_unsplit_workflow():
    jobs = _integration_jobs()
    executed = {"required": set(), "advisory": set()}
    for name, job in jobs.items():
        condition = re.search(r"(?m)^    if: (.*)$", job)
        condition = condition.group(1) if condition else ""
        if "inputs.mode != 'advisory'" in condition or name == "live-shard-times":
            executed["required"].add(name)
        elif "inputs.mode != 'required'" in condition:
            executed["advisory"].add(name)
        else:
            assert "inputs.mode" not in condition, (name, condition)
            executed["required"].add(name)
            executed["advisory"].add(name)
    assert executed["required"] | executed["advisory"] == (
        SKIPPED_ON_FORK_PRS | {"installation", "installation-windows"}
    )
    assert {"full", "managed", "managed-smart", "live-shard-times", "cujs"} <= executed["required"]
    assert {"soft-fail", "opencode", "copilot", "vscode", "managed-opencode"} <= executed[
        "advisory"
    ]

    matrix = re.search(r"(?m)^        include: (.*)$", jobs["anthropic-mps"]).group(1)
    required = re.search(r"inputs.mode == 'required' && '([^']+)'", matrix).group(1)
    advisory = re.search(r"inputs.mode == 'advisory' && '([^']+)'", matrix).group(1)
    all_modes = re.search(r"\|\| '([^']+)'\)\s*}}$", matrix).group(1)

    def canonical(value):
        return {json.dumps(item, sort_keys=True) for item in json.loads(value)}

    assert canonical(required) | canonical(advisory) == canonical(all_modes)
    assert not canonical(required) & canonical(advisory)


def test_integration_modes_preserve_blocking_and_advisory_lanes():
    jobs = _integration_jobs()
    required = {
        "installation",
        "installation-windows",
        "cowork-inference-windows",
        "cowork-inference-macos",
        "smoke",
        "headless-windows",
        "full",
        "codex-relay",
        "antigravity",
        "managed",
        "managed-smart",
        "dedicated-cuj-plan",
        "dedicated-cuj",
        "cujs",
        "codex-desktop-macos",
        "codex-desktop-windows",
    }
    advisory = {"soft-fail", "opencode", "copilot", "vscode", "managed-opencode"}
    for name in required:
        assert "inputs.mode != 'advisory'" in jobs[name], name
    for name in advisory:
        assert "inputs.mode != 'required'" in jobs[name], name
    assert "inputs.mode == 'required'" in jobs["anthropic-mps"]
    assert "inputs.mode == 'advisory'" in jobs["anthropic-mps"]
    assert "needs: [workspace, full, managed]" in jobs["soft-fail"]
    cujs_needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", jobs["cujs"])
    assert cujs_needs is not None
    assert {name.strip() for name in cujs_needs.group(1).split(",")} == {
        "installation",
        "installation-windows",
        "headless-windows",
        "cowork-inference-windows",
        "cowork-inference-macos",
        "workspace",
        "smoke",
        "full",
        "codex-relay",
        "anthropic-mps",
        "antigravity",
        "managed",
        "managed-smart",
        "codex-desktop-macos",
        "codex-desktop-windows",
        "dedicated-cuj-plan",
        "dedicated-cuj",
        "live-shard-times",
    }
    for name in required | advisory:
        condition = re.search(r"(?m)^    if: (.*)$", jobs[name]).group(1)
        if FORK_GUARD in condition:
            assert f"({FORK_GUARD})" in condition, name
    assert "default: all" in _workflow("integration.yml").split("  workflow_dispatch:", 1)[0]
