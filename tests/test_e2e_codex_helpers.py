import subprocess

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
