"""Offline collection contracts; generated cases must never execute or contact a workspace."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import run_integration as runner

SUITE = Path(__file__).parent / "e2e_integration"


@pytest.fixture
def discovery_suite(tmp_path):
    suite = tmp_path / "e2e_integration"
    shutil.copytree(SUITE, suite, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return suite


def add_cuj(suite, name, workspace, marks=("live", "claude"), extra=""):
    decorators = "\n".join(f"@pytest.mark.{mark}" for mark in marks)
    (suite / "tests" / f"test_cuj_{name}.py").write_text(
        "import pytest\nfrom helpers.base import BaseCujTest\n"
        f"{decorators}\nclass TestCuj{name.title()}(BaseCujTest):\n"
        f"    WORKSPACE_URL = {workspace!r}\n"
        f"{extra}"
        f"    def test_cuj_{name}(self):\n"
        "        raise AssertionError('Discovery must not execute journeys')\n"
    )


def collect(suite, *args):
    output = suite / "matrix.json"
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(suite / "pytest.ini"),
            f"--confcutdir={suite}",
            str(suite),
            "--collect-only",
            "--cuj-matrix-output",
            str(output),
            *args,
        ],
        cwd=suite.parent,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    return result, json.loads(output.read_text()) if output.exists() else None


def test_cuj_discovery_automatically_adds_exact_nodes_and_agent_requirements(discovery_suite):
    add_cuj(discovery_suite, "first", "https://first.example/")
    result, first = collect(discovery_suite)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(first["include"]) == 1
    entry = first["include"][0]
    assert entry["nodeid"] == "tests/test_cuj_first.py::TestCujFirst::test_cuj_first"
    assert entry["workspace"] == "https://first.example"
    assert entry["claude"] is True and entry["codex"] is False
    add_cuj(discovery_suite, "second", "https://second.example", ("live", "claude", "codex"))
    result, second = collect(discovery_suite)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(second["include"]) == 2
    assert second["include"][0] == entry
    assert second["include"][1]["codex"] is True
    for key in ("workspace_key", "artifact_key"):
        assert len({row[key] for row in second["include"]}) == 2


def test_cuj_discovery_workspace_key_does_not_depend_on_test_name(discovery_suite):
    add_cuj(discovery_suite, "first", "https://FIRST.example/")
    _, first = collect(discovery_suite)
    (discovery_suite / "tests/test_cuj_first.py").unlink()
    add_cuj(discovery_suite, "renamed", "https://first.example")
    _, renamed = collect(discovery_suite)
    assert first["include"][0]["workspace_key"] == renamed["include"][0]["workspace_key"]


@pytest.mark.parametrize(
    ("workspace", "marks", "message"),
    [
        ("", ("live", "claude"), "must declare WORKSPACE_URL"),
        ("https://first.example", ("claude",), "needs live and claude/codex"),
        ("https://first.example", ("live",), "needs live and claude/codex"),
    ],
)
def test_cuj_discovery_rejects_incomplete_metadata(discovery_suite, workspace, marks, message):
    add_cuj(discovery_suite, "first", workspace, marks)
    result, _ = collect(discovery_suite)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr


@pytest.mark.parametrize("second", ["https://first.example", "https://FIRST.example/"])
def test_cuj_discovery_rejects_shared_workspaces(discovery_suite, second):
    add_cuj(discovery_suite, "first", "https://first.example")
    add_cuj(discovery_suite, "second", second)
    result, _ = collect(discovery_suite)
    assert result.returncode != 0
    assert "separate" in result.stdout + result.stderr


def test_cuj_discovery_empty_scaffold_is_not_a_passing_run(discovery_suite):
    result, matrix = collect(discovery_suite)
    assert result.returncode == pytest.ExitCode.NO_TESTS_COLLECTED
    assert matrix == {"include": []}


def test_cuj_runner_resolves_exact_node():
    node = "tests/test_cuj_first.py::TestCujFirst::test_cuj_first"
    assert runner.cuj_test_target(node) == str(SUITE / node)


@pytest.mark.parametrize(
    "node",
    [
        "../integration/test_first.py::test_first",
        "tests/../test_cuj_first.py::test_first",
        "/tests/test_cuj_first.py::test_first",
        "tests/test_cuj_first.py",
        "tests/test_cuj_first.py::",
        "tests/test_cuj_first.py::TestCujFirst",
        "tests/test_cuj_first.py::TestCujFirst::other_method",
        "tests/test_first.py::test_first",
        "tests/test_cuj_first.py::test_first\n",
    ],
)
def test_cuj_runner_rejects_non_cuj_targets(node):
    with pytest.raises(ValueError, match="exact collected"):
        runner.cuj_test_target(node)


def test_cuj_runner_rejects_node_selection_for_legacy_suite():
    with pytest.raises(SystemExit):
        runner.arguments(
            ["--cuj-nodeid", "tests/test_cuj_first.py::TestCujFirst::test_cuj_first"],
            platform_name="posix",
            environment={},
        )
