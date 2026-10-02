"""Local workspace-declaration checks; no workspace calls or agent launches."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e_integration.helpers.base import BaseCujTest


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://workspace.test",
        "https://workspace.test/path",
        "https://workspace.test?x=1",
        "https://workspace.test#fragment",
        "https://user:secret@workspace.test",
        "https://",
        "https://workspace.test:invalid",
        "https://[invalid",
        " https://workspace.test",
    ],
)
def test_cuj_base_rejects_invalid_workspace_origins(url):
    cuj = type("TestCujExample", (BaseCujTest,), {"WORKSPACE_URL": url})
    with pytest.raises(ValueError, match="TestCujExample must declare WORKSPACE_URL"):
        _ = cuj().workspace_url


def test_cuj_base_normalizes_workspace_identity():
    cuj = type("TestCujExample", (BaseCujTest,), {"WORKSPACE_URL": "https://WORKSPACE.test:443/"})
    assert cuj().workspace_url == "https://workspace.test"


def test_cuj_base_requires_each_concrete_class_to_declare_its_workspace(monkeypatch):
    monkeypatch.setenv("UCODE_TEST_WORKSPACE", "https://global.test")
    parent = type("TestCujParent", (BaseCujTest,), {"WORKSPACE_URL": "https://parent.test"})
    child = type("TestCujChild", (parent,), {})
    with pytest.raises(ValueError, match="TestCujChild must declare WORKSPACE_URL"):
        _ = child().workspace_url
    with pytest.raises(ValueError, match="BaseCujTest must declare WORKSPACE_URL"):
        _ = BaseCujTest().workspace_url


@pytest.mark.parametrize(
    ("first_url", "second_url", "error"),
    [
        ("https://first.test", "https://second.test", None),
        ("https://WORKSPACE.test:443/", "https://workspace.test", "share a Databricks workspace"),
        ("https://first.test", "", "must declare WORKSPACE_URL"),
        ("https://first.test", "http://second.test", "must declare WORKSPACE_URL"),
    ],
)
def test_cuj_base_collection_checks_workspaces_before_execution(
    tmp_path, first_url, second_url, error
):
    source = Path(__file__).parent / "e2e_integration"
    suite = tmp_path / "suite"
    (suite / "helpers").mkdir(parents=True)
    (suite / "tests").mkdir()
    for name in ("conftest.py", "pytest.ini", "helpers/__init__.py", "helpers/base.py"):
        shutil.copyfile(source / name, suite / name)
    # Temporary local collection fixtures, never live CUJ implementations.
    (suite / "tests/test_cuj_example.py").write_text(
        "from helpers.base import BaseCujTest\n\n"
        "class TestCujFirst(BaseCujTest):\n"
        f"    WORKSPACE_URL = {first_url!r}\n"
        "    def test_cuj_first(self):\n"
        "        raise AssertionError('collection must not execute a journey')\n\n"
        "class TestCujSecond(BaseCujTest):\n"
        f"    WORKSPACE_URL = {second_url!r}\n"
        "    def test_cuj_second(self):\n"
        "        raise AssertionError('collection must not execute a journey')\n"
    )
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
            "--collect-only",
            str(suite),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    if error:
        assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
        assert error in result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 tests collected" in result.stdout
