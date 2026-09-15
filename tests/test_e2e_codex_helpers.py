import subprocess

import pytest

from tests import test_e2e as e2e
from ucode.agents import codex


def test_each_codex_model_is_explicitly_selected(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(e2e, "_require_binary", lambda _: None)
    monkeypatch.setattr(e2e, "_codex_home_outside_tmp", lambda: tmp_path / "home")
    monkeypatch.setattr(codex, "write_tool_config", lambda *a: None)

    def run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "hi", "")

    monkeypatch.setattr(e2e, "_run_agent", run)
    e2e.TestCodexLaunch().test_launch_codex_per_model(
        tmp_path,
        monkeypatch,
        {"codex_models": [
            "databricks-gpt-6-astra",
            "databricks-gpt-5-4",
            "databricks-gpt-5-5",
        ]},
        "https://example.com",
    )
    assert [cmd[1:3] for cmd in commands] == [
        ["--model", "gpt-5.4"],
        ["--model", "gpt-5.5"],
    ]


def test_known_catalog_404_is_separated_from_actual_failure():
    catalog_error = (
        "failed to refresh available models: unexpected status 404 Not Found: "
        '{"message":"codex/v1/models is not enabled for this workspace."}'
    )
    actual_error = "ERROR: exceeded retry limit, last status: 429 Too Many Requests"
    result = e2e._codex_failure_stderr(f"{catalog_error}\n{catalog_error}\n{actual_error}")
    assert actual_error in result
    assert catalog_error not in result
    assert "Nonfatal model discovery 404" in result


@pytest.mark.parametrize("error", [
    "ERROR: 404 Not Found from /responses",
    "ERROR: 403 Forbidden from /codex/v1/models",
])
def test_other_errors_remain_visible(error):
    result = e2e._codex_failure_stderr(error)
    assert error in result
    assert "Nonfatal" not in result
