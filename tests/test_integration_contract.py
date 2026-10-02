"""Keep the black-box suite independent of application internals and test doubles."""

import ast
import json
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
    assert "not workspace_isolated" in managed
    needs = re.search(r"(?m)^    needs: \[([^\]]+)\]$", gate)
    assert needs is not None
    assert {"cuj3", "managed"} <= {job.strip() for job in needs.group(1).split(",")}
    assert (
        "if: ${{ always() && (github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository) }}"
    ) in gate


def test_windows_integration_ci_uses_shared_claude_version():
    workflow = Path(__file__).parent.parent / ".github/workflows/integration.yml"
    contents = workflow.read_text()

    assert "  CLAUDE_VERSION: ${{ inputs.claude_version || '2.1.280' }}" in contents
    assert contents.count('"--claude-version", $env:CLAUDE_VERSION,') == 2


def test_cuj3_integration_ci_uses_the_dedicated_managed_workspace():
    workflow = Path(__file__).parent.parent / ".github/workflows/integration.yml"
    contents = workflow.read_text()
    cuj3 = contents.split("\n  cuj3:\n", 1)[1].split("\n  opencode:\n", 1)[0]

    assert "agent: [claude, codex]" in cuj3
    assert 'name: "CUJ3: model discovery' in cuj3
    assert "secrets.UG_CUJ3_WORKSPACE" in cuj3
    assert "secrets.UG_CUJ_SP_CLIENT_ID" in cuj3
    assert "secrets.UG_CUJ_SP_CLIENT_SECRET" in cuj3
    assert 'INSTALL_BOTH_AGENTS: "true"' in cuj3
    assert "TEST_MARKER: managed and cuj3 and workspace_isolated and ${{ matrix.agent }}" in cuj3
    assert "inputs.suite != 'tui'" in cuj3
    assert "inputs.suite != 'smoke'" in cuj3
    assert "inputs.suite != 'installation'" in cuj3
    assert "continue-on-error:" not in cuj3
    assert "fail-fast: false" in cuj3
    assert "steps: *live-steps" in cuj3
    assert 'if [[ "${INSTALL_BOTH_AGENTS:-}" == "true" ]]; then' in contents
    assert 'args=(--claude-version "$CLAUDE_VERSION" --codex-version "$CODEX_VERSION")' in contents


def test_cuj3_model_layer_configures_before_reading_persisted_policy():
    source = (Path(__file__).parent / "integration/test_ug_cuj3_model_discovery.py").read_text()
    assert "fetch_published_managed_config" not in source
    tree = ast.parse(source)
    journeys = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_case_")
    ]
    assert len(journeys) == 2
    for journey in journeys:
        configure_lines = [
            node.lineno
            for node in ast.walk(journey)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "configure"
        ]
        persisted_lines = [
            node.lineno
            for node in ast.walk(journey)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "assert_persisted_config"
        ]
        assert configure_lines and persisted_lines, journey.name
        assert max(configure_lines) < min(persisted_lines), journey.name


def test_cuj3_launches_defaults_without_model_overrides_and_checks_both_pickers():
    source = (Path(__file__).parent / "integration/test_ug_cuj3_model_discovery.py").read_text()
    tree = ast.parse(source)
    journeys = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_case_")
    }
    claude = journeys["test_case_03_managed_schema_pointers_claude"]
    codex = journeys["test_case_04_managed_schema_pointers_codex"]
    for journey, agent, task_name, default, prefix, picker in (
        (claude, "claude", "print_task", "CLAUDE_DEFAULT", ["claude", "-p"], "open_model_picker"),
        (
            codex,
            "codex",
            "exec_task",
            "CODEX_DEFAULT",
            ["codex", "--", "exec"],
            "open_codex_model_picker",
        ),
    ):
        calls = [node for node in ast.walk(journey) if isinstance(node, ast.Call)]
        commands = [
            node
            for node in calls
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and any(
                isinstance(argument, ast.Attribute)
                and argument.attr == "prompt"
                and isinstance(argument.value, ast.Name)
                and argument.value.id == task_name
                for argument in node.args
            )
        ]
        assert len(commands) == 1, journey.name
        command = commands[0]
        assert [ast.literal_eval(argument) for argument in command.args[: len(prefix)]] == prefix
        assert "--model" not in [
            argument.value for argument in command.args if isinstance(argument, ast.Constant)
        ]
        assert any(
            isinstance(node.func, ast.Name)
            and node.func.id == "assert_completed_task_model"
            and len(node.args) == 4
            and isinstance(node.args[2], ast.Attribute)
            and isinstance(node.args[2].value, ast.Name)
            and node.args[2].value.id == task_name
            and isinstance(node.args[3], ast.Name)
            and node.args[3].id == default
            for node in calls
        ), (agent, "Missing completed-task default-model evidence")
        assert any(
            isinstance(node.func, ast.Attribute) and node.func.attr == picker for node in calls
        ), (agent, "Missing native picker")
        assert any(
            isinstance(node.func, ast.Name) and node.func.id == "fetch_model_service_inventory"
            for node in calls
        ), (agent, "Missing independent fixture inventory")
        assert any(
            isinstance(node.func, ast.Name) and node.func.id == "assert_picker_inventory"
            for node in calls
        ), (agent, "Missing exact numbered picker inventory")
    for journey, agent, command, task_name, default in (
        (claude, "claude", "[str(session.binary)]", "default_task", "CLAUDE_DEFAULT"),
        (claude, "claude", "[str(session.binary), 'claude']", "explicit_task", "CLAUDE_DEFAULT"),
        (codex, "codex", "[str(session.binary), 'codex']", "default_task", "CODEX_DEFAULT"),
    ):
        terminals = [
            node
            for node in journey.body
            if isinstance(node, ast.With)
            and len(node.items) == 1
            and isinstance(node.items[0].context_expr, ast.Call)
            and isinstance(node.items[0].context_expr.func, ast.Name)
            and node.items[0].context_expr.func.id == "AgentTerminal"
            and len(node.items[0].context_expr.args) >= 3
            and ast.unparse(node.items[0].context_expr.args[1]) == repr(agent)
            and ast.unparse(node.items[0].context_expr.args[2]) == command
        ]
        assert len(terminals) == 1, (agent, command, "Missing exact interactive launch")
        terminal = terminals[0]
        terminal_name = ast.unparse(terminal.items[0].optional_vars)
        lifecycle_calls = [
            node.value
            for node in terminal.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and ast.unparse(node.value.func.value) == terminal_name
            and node.value.func.attr in {"boot", "submit", "wait_for_task", "exit_normally"}
        ]
        assert [node.func.attr for node in lifecycle_calls] == [
            "boot",
            "submit",
            "wait_for_task",
            "exit_normally",
        ], (agent, command, "Missing ordered TUI task lifecycle")
        assert [ast.unparse(arg) for arg in lifecycle_calls[1].args] == [f"{task_name}.prompt"]
        assert [ast.unparse(arg) for arg in lifecycle_calls[2].args] == [task_name]
        task_definitions = [
            node
            for node in journey.body
            if isinstance(node, ast.Assign)
            and any(ast.unparse(target) == task_name for target in node.targets)
        ]
        assert len(task_definitions) == 1, (agent, command, "Requires a separate file task")
        task_definition = task_definitions[0]
        assert ast.unparse(task_definition.value) == "FileTask(session)"
        assert task_definition.lineno < terminal.lineno
        evidence = [
            ast.unparse(node.value)
            for node in journey.body
            if isinstance(node, ast.Expr) and node.lineno > terminal.end_lineno
        ]
        assert f"{task_name}.assert_completed(session, {agent!r})" in evidence, (
            agent,
            command,
            "Missing completed native assistant answer",
        )
        assert (
            f"assert_completed_task_model(session, {agent!r}, {task_name}.value, {default})"
            in evidence
        ), (agent, command, "Missing exact completed-task default-model evidence")


@pytest.mark.parametrize("suite", ["full", "live", "smoke", "tui", "installation"])
@pytest.mark.parametrize("managed_result", ["success", "failure", "cancelled", "skipped"])
def test_integration_ci_gate_requires_selected_managed_jobs(suite, managed_result):
    workflow = Path(__file__).parent.parent / ".github/workflows/integration.yml"
    gate = workflow.read_text().split("\n  cujs:\n", 1)[1]
    script = re.search(r"          python3 - <<'PY'\n(.*?)          PY", gate, re.DOTALL)
    assert script is not None
    results = {
        job: {"result": "success"}
        for job in ("installation", "workspace", "smoke", "full", "cuj3", "managed")
    }
    results["managed"]["result"] = managed_result
    for job in {
        "installation": ("workspace", "smoke", "full", "cuj3"),
        "smoke": ("full", "cuj3"),
        "tui": ("smoke", "cuj3"),
    }.get(suite, ()):
        results[job]["result"] = "skipped"
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script.group(1))],
        env={"RESULTS": json.dumps(results), "SUITE": suite},
        capture_output=True,
        text=True,
        timeout=10,
    )
    if suite in {"full", "live"} and managed_result != "success":
        assert result.returncode != 0
        assert "Integration jobs did not pass: managed" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "All selected integration jobs passed:" in result.stdout


@pytest.mark.parametrize("suite", ["full", "live", "smoke", "tui", "installation"])
@pytest.mark.parametrize("cuj3_result", ["success", "failure", "cancelled", "skipped"])
def test_integration_ci_gate_requires_selected_cuj3_jobs(suite, cuj3_result):
    workflow = Path(__file__).parent.parent / ".github/workflows/integration.yml"
    gate = workflow.read_text().split("\n  cujs:\n", 1)[1]
    script = re.search(r"          python3 - <<'PY'\n(.*?)          PY", gate, re.DOTALL)
    assert script is not None
    results = {
        job: {"result": "success"}
        for job in ("installation", "workspace", "smoke", "full", "cuj3", "managed")
    }
    results["cuj3"]["result"] = cuj3_result
    for job in {
        "installation": ("workspace", "smoke", "full", "cuj3"),
        "smoke": ("full", "cuj3"),
        "tui": ("smoke", "cuj3"),
    }.get(suite, ()):
        results[job]["result"] = "skipped"
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script.group(1))],
        env={"RESULTS": json.dumps(results), "SUITE": suite},
        capture_output=True,
        text=True,
        timeout=10,
    )
    if suite in {"full", "live"} and cuj3_result != "success":
        assert result.returncode != 0
        assert "Integration jobs did not pass: cuj3" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "All selected integration jobs passed:" in result.stdout


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
                if marks & {"live", "managed", "workspace_switch"}:
                    assert len(marks & {"claude", "codex", "opencode"}) == 1, node.name


def test_model_discovery_cases_match_current_launch_contract():
    root = Path(__file__).parent / "integration"
    seen = []
    for path in root.glob("test_ug_*_model_discovery.py"):
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
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            match = re.match(r"test_case_(\d{2})_", node.name)
            if match is None:
                continue
            case = int(match.group(1))
            seen.append(case)
            marks = module_marks | _markers(node.decorator_list)
            expected = (
                {"managed"} if case in {3, 4} else ({"managed_fixture"} if case <= 6 else {"live"})
            )
            assert marks & {"managed_fixture", "managed", "live"} == expected, node.name
            if case in {3, 4}:
                assert {"managed", "cuj3", "workspace_isolated"} <= marks, node.name
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
    assert len(seen) == 22
    for case in expected_cases:
        assert seen.count(case) == (1 if case in {3, 4} or 7 <= case <= 10 else 2), case


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
