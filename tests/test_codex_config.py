from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
import tomlkit

from ucode import codex_config
from ucode.agents import codex
from ucode.codex_config import codex_config_args

WS = "https://example.databricks.com"


class TestCodexConfigArgs:
    def test_layers_provider_overrides_without_replacing_user_config(self, monkeypatch):
        monkeypatch.setattr(codex, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.148.0")

        overlay = codex.render_overlay(
            WS,
            "gpt-5.6-luna",
            "myprof",
        )
        args = codex_config_args(overlay)

        assert args[:4] == [
            "--config",
            'model_provider="Databricks"',
            "--config",
            'model="gpt-5.6-luna"',
        ]
        provider_override = args[-1]
        assert provider_override.startswith("model_providers.Databricks={")
        assert "/ai-gateway/codex/v1" in provider_override
        assert 'command = "' in provider_override
        assert '"myprof"' in provider_override

    def test_renders_nested_tables_from_parsed_profile(self):
        profile = tomlkit.parse(
            """
model_provider = "ucode-databricks"

[model_providers.ucode-databricks]
name = "Databricks AI Gateway"

[model_providers.ucode-databricks.http_headers]
User-Agent = "ucode"

[model_providers.ucode-databricks.auth]
command = "ucode"
args = ["codex-token"]

[tui.model_availability_nux]
"gpt-5.6-sol" = 1
"""
        )

        args = codex_config_args(profile)

        provider_override = next(
            arg for arg in args if arg.startswith("model_providers.ucode-databricks=")
        )
        assert 'http_headers = {User-Agent = "ucode"}' in provider_override
        assert 'auth = {command = "ucode", args = ["codex-token"]}' in provider_override
        assert 'tui={model_availability_nux = {"gpt-5.6-sol" = 1}}' in args

    def test_renders_native_hook_defaults_as_optional_fields(self):
        config = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": None,
                        "hooks": [
                            {"type": "command", "command": "policy", "command_windows": None}
                        ],
                    }
                ]
            }
        }

        args = codex_config_args(config)

        assert tomlkit.parse(args[1]) == {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [{"type": "command", "command": "policy"}],
                    }
                ]
            }
        }


@pytest.mark.parametrize(
    "args",
    [
        ["--cd", "project"],
        ["--cd=project"],
        ["-C", "project"],
        ["-Cproject"],
        ["-C=project"],
        ["--cd", "wrong", "--cd", "project", "--", "--cd", "ignored"],
    ],
)
def test_codex_working_directory(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    assert codex_config.codex_working_directory(args) == tmp_path / "project"


def test_native_lookup_preserves_caller_config_overrides():
    overrides = [
        "-c",
        'projects={"/project"={trust_level="untrusted"}}',
        "--config",
        'model="example"',
        "--config=features.hooks=true",
        "-cfeatures.search=false",
        "--disable",
        "remote_control",
    ]
    args = ["--cd", "/project", *overrides, "--", "--config=prompt-text"]

    assert codex_config.codex_cli_config_args(args) == overrides
    assert args[-1] == "--config=prompt-text"


class TestReadEffectiveCodexConfig:
    def _server(self, monkeypatch, script):
        processes = []
        calls = []

        def start(argv, **kwargs):
            # Substitute only the native protocol peer; exercise real pipes and cleanup.
            calls.append((argv, kwargs))
            process = subprocess.Popen([sys.executable, "-c", textwrap.dedent(script)], **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(codex_config.subprocess_cross_os, "popen", start)
        return processes, calls

    def test_reads_native_result_after_handshake(self, tmp_path, monkeypatch, capsys):
        config = {"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "project-policy"}]}]}}
        script = """
            import json, sys
            initial = json.loads(sys.stdin.readline())
            assert initial['method'] == 'initialize'
            print(json.dumps({'id': initial['id'], 'result': {}}), flush=True)
            assert json.loads(sys.stdin.readline())['method'] == 'initialized'
            request = json.loads(sys.stdin.readline())
            assert request['method'] == 'config/read'
            assert request['params'] == {'cwd': CWD, 'includeLayers': False}
            print(json.dumps({'method': 'notification'}), flush=True)
            print(json.dumps({'id': request['id'], 'result': {'config': CONFIG}}), flush=True)
            assert sys.stdin.read() == ''
        """.replace("CWD", repr(str(tmp_path))).replace("CONFIG", repr(config))
        processes, calls = self._server(monkeypatch, script)

        assert (
            codex_config.read_effective_codex_config(
                "/selected/codex", cwd=tmp_path, config_args=["--config", 'model="example"']
            )
            == config
        )

        assert calls[0][0] == [
            "/selected/codex",
            "app-server",
            "--config",
            'model="example"',
            "--listen",
            "stdio://",
        ]
        assert calls[0][1]["cwd"] == tmp_path
        assert processes[0].returncode == 0
        assert processes[0].stdin.closed and processes[0].stdout.closed
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize(
        "response",
        [
            "not json",
            json.dumps({"id": 1, "error": {"message": "invalid config"}}),
            json.dumps({"id": 1, "result": None}),
        ],
    )
    def test_protocol_errors_do_not_fall_back_to_incomplete_hooks(
        self, tmp_path, monkeypatch, response
    ):
        processes, _ = self._server(
            monkeypatch,
            f"""
            import sys
            sys.stdin.readline()
            print({response!r}, flush=True)
        """,
        )

        with pytest.raises(RuntimeError, match="--disable-smart-routing"):
            codex_config.read_effective_codex_config("codex", cwd=tmp_path, config_args=[])

        assert processes[0].poll() is not None

    def test_unresponsive_server_is_reaped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(codex_config, "CONFIG_READ_TIMEOUT_SECONDS", 0.05)
        processes, _ = self._server(monkeypatch, "import time; time.sleep(60)")

        with pytest.raises(RuntimeError, match="Could not read Codex configuration"):
            codex_config.read_effective_codex_config("codex", cwd=tmp_path, config_args=[])

        assert processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
