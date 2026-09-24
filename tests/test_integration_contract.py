"""Keep the black-box suite independent of application internals and test doubles."""

import ast
import re
from pathlib import Path

import pytest

from tests.integration.utils.evidence import assert_opencode_answer, opencode_completed_session
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


def test_live_integration_cases_belong_to_exactly_one_selected_agent():
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


@pytest.fixture
def opencode_export():
    """A parser input, never a substitute for a live integration transcript."""
    return {
        "info": {"id": "ses_test"},
        "messages": [
            {
                "info": {
                    "role": "assistant",
                    "sessionID": "ses_test",
                    "providerID": "databricks-oss",
                    "modelID": "catalog.schema.model",
                    "finish": "stop",
                    "time": {"completed": 1},
                },
                "parts": [
                    {
                        "type": "tool",
                        "state": {"status": "completed", "output": "file-value\ndone"},
                    },
                    {"type": "text", "text": "file-value\ndone"},
                ],
            }
        ],
    }


def test_opencode_evidence_requires_a_completed_native_session(opencode_export):
    output = 'Launching OpenCode\n{"type":"step_finish","sessionID":"ses_test","part":{"reason":"stop"}}\n'
    assert opencode_completed_session(output) == "ses_test"
    assert_opencode_answer(
        opencode_export, "ses_test", "catalog.schema.model", "file-value\ndone\n"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "user"),
        ("sessionID", "ses_other"),
        ("modelID", "catalog.schema.other"),
        ("providerID", "openai"),
        ("finish", "tool-calls"),
        ("time", {}),
        ("error", {"name": "APIError"}),
    ],
)
def test_opencode_evidence_rejects_wrong_or_incomplete_assistant_messages(
    opencode_export, field, value
):
    opencode_export["messages"][0]["info"][field] = value
    with pytest.raises(AssertionError):
        assert_opencode_answer(
            opencode_export, "ses_test", "catalog.schema.model", "file-value\ndone"
        )


@pytest.mark.parametrize("evidence", ["tool_only", "text_only", "synthetic", "wrong_answer"])
def test_opencode_evidence_rejects_tool_output_or_echoes_as_final_answers(
    opencode_export, evidence
):
    parts = opencode_export["messages"][0]["parts"]
    if evidence == "tool_only":
        del parts[1]
    elif evidence == "text_only":
        del parts[0]
    elif evidence == "synthetic":
        parts[1]["synthetic"] = True
    else:
        parts[1]["text"] = "Read the file and append done."
    with pytest.raises(AssertionError):
        assert_opencode_answer(
            opencode_export, "ses_test", "catalog.schema.model", "file-value\ndone"
        )


@pytest.mark.parametrize(
    "output",
    [
        "Launching OpenCode",
        '{"type":"text","sessionID":"ses_test","part":{"text":"file-value"}}',
        '{"type":"step_finish","sessionID":"ses_test","part":{"reason":"tool-calls"}}',
        '{"type":"error","sessionID":"ses_test"}\n'
        '{"type":"step_finish","sessionID":"ses_test","part":{"reason":"stop"}}',
    ],
)
def test_opencode_evidence_rejects_runs_without_successful_completion(output):
    with pytest.raises(AssertionError):
        opencode_completed_session(output)


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
            expected = {"managed_fixture"} if case <= 6 else {"live"}
            assert marks & {"managed_fixture", "managed", "live"} == expected, node.name
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
