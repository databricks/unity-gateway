"""Omnigent command dispatch and terminal handoff coverage."""

import os
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ucode.cli import app


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--help"],
        ["--version"],
        ["--debug", "run", "--harness", "codex", "-p", "hello world"],
        ["codex", "--", "--help"],
        ["--", "--help"],
        ["claude", "--", "-p", 'keep "quotes" & pipes | and $HOME literal\nnext line'],
        ["codex", "--model=example-model", "", "--", "--", "-p"],
    ],
)
def test_omni_forwards_arguments(args):
    with (
        patch("ucode.agents.omni.shutil.which", return_value="/path with spaces/omnigent"),
        patch("ucode.agents.omni.exec_or_spawn") as launch,
        patch("ucode.cli._launch_tool") as configure_agent,
    ):
        result = CliRunner().invoke(app, ["omni", *args])
    assert result.exit_code == 0, result.output
    launch.assert_called_once_with(["/path with spaces/omnigent", *args])
    configure_agent.assert_not_called()


def test_omni_missing_install():
    with (
        patch("ucode.agents.omni.shutil.which", return_value=None),
        patch("ucode.agents.omni.exec_or_spawn") as launch,
    ):
        result = CliRunner().invoke(app, ["omni"])
    assert result.exit_code == 1
    assert "uv tool install" in result.output
    launch.assert_not_called()


@pytest.mark.parametrize("error", [PermissionError("denied"), FileNotFoundError("removed")])
def test_omni_launch_failure(error):
    with (
        patch("ucode.agents.omni.shutil.which", return_value="/bin/omnigent"),
        patch("ucode.agents.omni.exec_or_spawn", side_effect=error),
    ):
        result = CliRunner().invoke(app, ["omni"])
    assert result.exit_code == 1
    assert "Could not launch Omnigent" in result.output


def test_omni_propagates_child_exit_status():
    with (
        patch("ucode.agents.omni.shutil.which", return_value="/bin/omnigent"),
        patch("ucode.agents.omni.exec_or_spawn", side_effect=SystemExit(42)),
    ):
        result = CliRunner().invoke(app, ["omni"])
    assert result.exit_code == 42
    assert "Could not launch" not in result.output


def test_omni_preserves_session_environment_and_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_CONFIG", "/example/config.yaml")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "example-profile")

    def handoff(argv):
        assert os.getcwd() == str(tmp_path)
        assert os.environ["OMNIGENT_CONFIG"] == "/example/config.yaml"
        assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "example-profile"

    with (
        patch("ucode.agents.omni.shutil.which", return_value="/bin/omnigent"),
        patch("ucode.agents.omni.exec_or_spawn", side_effect=handoff) as launch,
    ):
        result = CliRunner().invoke(app, ["omni"])
    assert result.exit_code == 0, result.output
    launch.assert_called_once()


def test_top_level_help_lists_omni_without_an_installation():
    with patch("ucode.agents.omni.shutil.which", return_value=None):
        result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "omni" in result.output
    assert "Launch Omnigent" in result.output
