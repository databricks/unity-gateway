"""Local contracts for the independent full E2E CUJ suite; no live calls."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import run_integration as runner

SUITE = Path(__file__).parent / "e2e_integration"


def test_cuj_e2e_naming_and_scenarios():
    for path in SUITE.rglob("test_*.py"):
        assert path.name.startswith("test_cuj_"), path
        assert path.parent == SUITE / "tests", "Keep journeys in tests/ and helpers in helpers/."
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                assert node.name.startswith("test_cuj_"), (path, node.name)
                description = ast.get_docstring(node) or ""
                assert "Scenario:" in description and "Expected:" in description, node.name


def test_cuj_e2e_has_no_stubs_or_legacy_imports():
    forbidden_names = {"monkeypatch", "MonkeyPatch", "Mock", "MagicMock", "patch"}
    forbidden_attributes = forbidden_names | {"mock", "skip", "skipif", "xfail"}
    for path in SUITE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            for module in modules:
                assert module.split(".")[0] not in {"ucode", "mock", "unittest", "utils"}, (
                    path,
                    module,
                )
                assert not module.startswith(("tests.integration", "tests.test_e2e")), (
                    path,
                    module,
                )
            if isinstance(node, ast.Name):
                assert node.id not in forbidden_names, (path, node.lineno)
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_attributes, (path, node.lineno)
            if isinstance(node, ast.Constant):
                assert node.value != "UCODE_MANAGED_CONFIG_STUB", (path, node.lineno)


@pytest.mark.parametrize("isolated", [True, False])
def test_cuj_e2e_collection_requires_its_own_fixture_boundary(tmp_path, isolated):
    command = [sys.executable, "-m", "pytest", "-c", str(SUITE / "pytest.ini")]
    if isolated:
        command.append(f"--confcutdir={SUITE}")
    command.extend(
        [
            str(SUITE),
            "--collect-only",
            "--trace-config",
            "-o",
            f"cache_dir={tmp_path / 'cache'}",
        ]
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    if isolated:
        # The documentation-only scaffold is deliberately an empty selection,
        # not a successful live run. Update this when real journeys are added.
        assert result.returncode == pytest.ExitCode.NO_TESTS_COLLECTED, (
            result.stdout + result.stderr
        )
        assert "no tests collected" in result.stdout
        assert str(SUITE / "conftest.py") in result.stdout
        assert str(SUITE.parent / "conftest.py") not in result.stdout
        assert str(SUITE.parent / "integration/conftest.py") not in result.stdout
    else:
        assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
        assert "Full E2E CUJs require --confcutdir" in result.stderr


def test_cuj_e2e_is_excluded_from_ordinary_collection():
    config = ast.parse((SUITE.parent / "conftest.py").read_text())
    ignored = next(
        ast.literal_eval(node.value)
        for node in config.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "collect_ignore"
            for target in node.targets
        )
    )
    assert "e2e_integration" in ignored


def test_cuj_e2e_runner_targets_only_the_selected_suite():
    assert runner.SUITES["e2e-integration"] == SUITE
    assert runner.integration_test_targets(
        runner.SUITES["e2e-integration"],
        ["claude", "codex"],
        platform_name="posix",
        installation_only=False,
        headless_only=False,
    ) == [str(SUITE)]
