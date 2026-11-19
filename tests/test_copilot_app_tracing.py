"""The GitHub Copilot app's own tracing: its span file converted to OTLP/JSON, the login
service's tailer, and the variables that turn the app's file exporter on."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ucode import copilot_app_tracing, copilot_otel_file, shell_profile_env
from ucode.managed_files import OS
from ucode.os_compatibility import windows_user_env

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "copilot_app_spans.jsonl"


def _attrs(key_values: list[dict]) -> dict:
    return {kv["key"]: kv["value"] for kv in key_values}


class TestConverter:
    def test_golden_sample(self):
        payload = copilot_otel_file.to_otlp_json(SAMPLE.read_bytes().splitlines())

        assert payload == json.loads((FIXTURES / "copilot_app_spans.otlp.json").read_text())

    def test_keeps_resource_scope_and_genai_attributes(self):
        payload = copilot_otel_file.to_otlp_json(SAMPLE.read_bytes().splitlines())

        assert payload is not None
        [resource_spans] = payload["resourceSpans"]
        assert _attrs(resource_spans["resource"]["attributes"])["service.name"] == {
            "stringValue": "github-copilot"
        }
        assert resource_spans["schemaUrl"] == "https://opentelemetry.io/schemas/1.45.0"
        [scope_spans] = resource_spans["scopeSpans"]
        assert scope_spans["scope"] == {"name": "github.copilot", "version": "1.0.106-8"}
        timing, chat, agent = scope_spans["spans"]
        assert timing["name"] == "session.timing.construction_input"
        # The JS SDK's CLIENT (2) and INTERNAL (0) are OTLP's 3 and 1.
        assert (chat["kind"], agent["kind"]) == (3, 1)
        assert chat["startTimeUnixNano"] == "1795122228955000000"
        assert chat["parentSpanId"] == agent["spanId"]
        attributes = _attrs(chat["attributes"])
        assert attributes["gen_ai.request.model"] == {"stringValue": "claude-haiku-5.5"}
        assert attributes["gen_ai.usage.input_tokens"] == {"intValue": "160"}
        assert attributes["gen_ai.request.stream"] == {"boolValue": True}
        assert attributes["github.copilot.cost"] == {"doubleValue": 1.0}
        assert attributes["gen_ai.response.finish_reasons"] == {
            "arrayValue": {"values": [{"stringValue": "stop"}]}
        }
        assert {"gen_ai.conversation.id", "github.copilot.turn_id"} <= attributes.keys()
        assert chat["status"] == {"code": 0}
        assert [event["name"] for event in agent["events"]][2] == "github.copilot.user.message"
        assert agent["events"][0]["timeUnixNano"] == "1795122228953000000"

    def test_skips_metrics_and_unreadable_lines(self):
        lines = [b'{"type":"metric","name":"m"}', b"not json", b"[]"]

        assert copilot_otel_file.to_otlp_json(lines) is None

    def test_skips_a_malformed_span_and_keeps_the_rest(self):
        good = SAMPLE.read_bytes().splitlines()[0]
        lines = [
            b'{"type":"span","name":"a","kind":"server"}',
            b'{"type":"span","name":"b","links":[{"context":"x"}]}',
            b"\0\0\0" + good,
        ]

        payload = copilot_otel_file.to_otlp_json(lines)

        spans = [
            s for rs in payload["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]
        ]
        assert len(spans) == 1

    def test_groups_spans_by_resource(self):
        def span(service: str) -> bytes:
            return json.dumps(
                {"type": "span", "name": "s", "resource": {"attributes": {"service.name": service}}}
            ).encode()

        payload = copilot_otel_file.to_otlp_json([span("a"), span("b"), span("a")])

        assert payload is not None
        assert [len(group["scopeSpans"][0]["spans"]) for group in payload["resourceSpans"]] == [
            2,
            1,
        ]


SPAN = b'{"type":"span","traceId":"t","spanId":"s","name":"chat","kind":2}\n'
METRIC = b'{"type":"metric","name":"m"}\n'


class TestTailer:
    @pytest.fixture
    def sent(self):
        return []

    def _tailer(self, path: Path, sent: list, results: list[bool] | None = None, **kwargs):
        outcomes = iter(results or [])

        def send(body: bytes) -> bool:
            sent.append(json.loads(body))
            return next(outcomes, True)

        return copilot_otel_file.SpanFileTailer(path, send, **kwargs)

    def test_forwards_complete_lines_and_empties_the_file(self, tmp_path, sent):
        path = tmp_path / "spans.jsonl"
        path.write_bytes(SPAN + METRIC + SPAN[:20])
        tailer = self._tailer(path, sent)

        tailer.poll()

        assert len(sent) == 1
        assert len(sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 1
        # The partial line waits for the runtime to finish it, so the file isn't emptied yet.
        assert path.read_bytes() == SPAN + METRIC + SPAN[:20]
        with path.open("ab") as file:
            file.write(SPAN[20:])
        tailer.poll()
        assert len(sent) == 2
        assert path.read_bytes() == b""

    def test_metric_only_lines_are_consumed_without_a_post(self, tmp_path, sent):
        path = tmp_path / "spans.jsonl"
        path.write_bytes(METRIC * 3)

        self._tailer(path, sent).poll()

        assert sent == []
        assert path.read_bytes() == b""

    def test_a_refused_batch_is_retried_then_dropped(self, tmp_path, sent):
        path = tmp_path / "spans.jsonl"
        path.write_bytes(SPAN)
        dropped = []
        tailer = self._tailer(
            path, sent, [False, False, False], max_attempts=3, on_drop=dropped.append
        )

        tailer.poll()
        tailer.poll()
        assert path.read_bytes() == SPAN
        assert dropped == []
        tailer.poll()

        assert len(sent) == 3
        assert dropped == ["a batch the gateway kept refusing"]
        assert path.read_bytes() == b""

    def test_a_shrunk_file_is_read_from_the_start(self, tmp_path, sent):
        path = tmp_path / "spans.jsonl"
        path.write_bytes(SPAN * 2 + b'{"type":"span"')
        tailer = self._tailer(path, sent)
        tailer.poll()
        assert len(sent) == 1

        path.write_bytes(SPAN)
        tailer.poll()

        assert len(sent) == 2

    def test_bounded_batches(self, tmp_path, sent):
        path = tmp_path / "spans.jsonl"
        path.write_bytes(SPAN * 10)

        self._tailer(path, sent, max_batch_bytes=len(SPAN) * 3).poll()

        assert [len(body["resourceSpans"][0]["scopeSpans"][0]["spans"]) for body in sent] == [
            3,
            3,
            3,
            1,
        ]
        assert path.read_bytes() == b""

    def test_a_line_larger_than_a_batch_is_skipped(self, tmp_path, sent):
        path = tmp_path / "spans.jsonl"
        path.write_bytes(b"x" * 100 + b"\n" + SPAN)
        dropped = []

        self._tailer(path, sent, max_batch_bytes=len(SPAN) + 10, on_drop=dropped.append).poll()

        assert dropped == ["a span line too large to send"]
        assert len(sent) == 1

    def test_no_file_yet(self, tmp_path, sent):
        self._tailer(tmp_path / "missing.jsonl", sent).poll()

        assert sent == []


class _FakeGateway(BaseHTTPRequestHandler):
    received: list = []
    statuses: list = []

    def do_POST(self):  # noqa: N802 - http.server's naming
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((self.path, dict(self.headers), json.loads(body)))
        status = type(self).statuses.pop(0) if type(self).statuses else 200
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class TestForwardingEndToEnd:
    def test_spans_reach_the_gateway_with_a_fresh_token_and_ug_headers(self, tmp_path):
        _FakeGateway.received = []
        _FakeGateway.statuses = [401]
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tokens = iter(["stale", "fresh"])
        current = {"token": next(tokens)}

        def refresh():
            current["token"] = next(tokens)

        path = tmp_path / "spans.jsonl"
        path.write_bytes(SAMPLE.read_bytes())
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}/") as client:
                send = copilot_otel_file.gateway_sender(
                    client,
                    lambda: current["token"],
                    refresh,
                    "/ai-gateway/otel/v1/traces",
                    {"User-Agent": "copilot/1.0"},
                )
                copilot_otel_file.SpanFileTailer(path, send).poll()
        finally:
            server.shutdown()
            server.server_close()

        assert [request[0] for request in _FakeGateway.received] == [
            "/ai-gateway/otel/v1/traces",
            "/ai-gateway/otel/v1/traces",
        ]
        _path, headers, body = _FakeGateway.received[1]
        assert headers["Authorization"] == "Bearer fresh"
        assert headers["Content-Type"] == "application/json"
        assert headers["User-Agent"] == "copilot/1.0"
        assert len(body["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 3
        assert path.read_bytes() == b""

    def test_a_dead_login_refuses_the_batch(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        def refresh():
            raise RuntimeError("sign in")

        with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://w/") as client:
            send = copilot_otel_file.gateway_sender(client, lambda: "t", refresh, "/p", {})
            assert send(b"{}") is False


ENV = {"COPILOT_OTEL_ENABLED": "true", "COPILOT_OTEL_FILE_EXPORTER_PATH": "/h/it's/spans.jsonl"}


@pytest.fixture
def home():
    return shell_profile_env._home()


class TestProfileFiles:
    def test_bash_uses_the_first_login_file_that_exists(self, home):
        home.mkdir(parents=True)
        assert shell_profile_env.profile_file("bash") == home / ".profile"
        (home / ".bash_login").write_text("")
        assert shell_profile_env.profile_file("bash") == home / ".bash_login"
        (home / ".bash_profile").write_text("")
        assert shell_profile_env.profile_file("bash") == home / ".bash_profile"

    def test_zsh_sh_pwsh_and_fish(self, home, monkeypatch):
        assert shell_profile_env.profile_file("zsh") == home / ".zshenv"
        for shell in ("sh", "dash", "pwsh"):
            assert shell_profile_env.profile_file(shell) == home / ".profile"
        assert shell_profile_env.profile_file("fish") == (
            home / ".config" / "fish" / "conf.d" / "unity-gateway.fish"
        )
        monkeypatch.setattr(shell_profile_env, "_system_zdotdir", lambda: "/etc/zdot")
        assert shell_profile_env.profile_file("zsh") == Path("/etc/zdot/.zshenv")

    @pytest.mark.parametrize("shell", ["ksh", "mksh", "elvish", "tcsh"])
    def test_shells_the_app_doesnt_probe(self, shell):
        assert shell_profile_env.profile_file(shell) is None


class TestProfileBlock:
    def test_write_is_idempotent_and_remove_restores_the_file(self, tmp_path):
        path = tmp_path / ".zshenv"
        original = "export EDITOR=vim\n"
        path.write_text(original)

        assert shell_profile_env.write(path, ENV)
        assert not shell_profile_env.write(path, ENV)
        text = path.read_text()
        assert text.startswith(original)
        assert text.count(shell_profile_env.BEGIN) == 1
        assert 'if [ -n "${COPILOT_RESOLVING_ENVIRONMENT:-}" ]; then' in text

        assert shell_profile_env.write(path, {**ENV, "COPILOT_OTEL_ENABLED": "false"})
        assert path.read_text().count(shell_profile_env.BEGIN) == 1

        assert shell_profile_env.remove(path)
        assert path.read_text() == original
        assert not shell_profile_env.remove(path)

    def test_goes_first_in_a_profile_that_can_return_early(self, tmp_path):
        path = tmp_path / ".profile"
        path.write_text('[ -z "$PS1" ] && return\nexport A=1\n')

        shell_profile_env.write(path, ENV)

        assert path.read_text().startswith(shell_profile_env.BEGIN)
        shell_profile_env.remove(path)
        assert path.read_text() == '[ -z "$PS1" ] && return\nexport A=1\n'

    def test_fish_gets_its_own_file_which_remove_deletes(self, home):
        path = shell_profile_env.profile_file("fish")
        assert path is not None

        shell_profile_env.write(path, ENV)

        assert "if set -q COPILOT_RESOLVING_ENVIRONMENT" in path.read_text()
        assert "set -gx COPILOT_OTEL_FILE_EXPORTER_PATH '/h/it\\'s/spans.jsonl'" in path.read_text()
        shell_profile_env.remove(path)
        assert not path.exists()

    @pytest.mark.parametrize("shell", ["bash", "sh", "fish"])
    def test_only_the_apps_probe_sees_the_variables(self, shell, home, tmp_path):
        binary = shutil.which(shell)
        if binary is None or sys.platform == "win32":
            pytest.skip(f"{shell} isn't installed")
        path = shell_profile_env.profile_file(shell)
        assert path is not None
        shell_profile_env.write(path, ENV)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": str(home / ".config")}
        flags = ["-l", "-c"]

        def probe(extra: dict) -> str:
            return subprocess.run(
                [binary, *flags, "echo $COPILOT_OTEL_FILE_EXPORTER_PATH"],
                env={**env, **extra},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            ).stdout.strip()

        assert (
            probe({"COPILOT_RESOLVING_ENVIRONMENT": "1"}) == ENV["COPILOT_OTEL_FILE_EXPORTER_PATH"]
        )
        assert probe({}) == ""


class TestAppTracingVariables:
    @pytest.fixture(autouse=True)
    def _linux(self, monkeypatch):
        monkeypatch.setattr(copilot_app_tracing, "current_os", lambda: OS.LINUX)

    def test_enable_writes_each_login_shells_profile_and_disable_removes_it(
        self, home, monkeypatch
    ):
        monkeypatch.setattr(shell_profile_env, "login_shells", lambda: ["bash", "zsh"])
        home.mkdir(parents=True)
        (home / ".zshenv").write_text("export A=1\n")

        assert copilot_app_tracing.enable()
        assert not copilot_app_tracing.enable()
        assert str(copilot_otel_file.SPAN_FILE) in (home / ".profile").read_text()
        assert shell_profile_env.BEGIN in (home / ".zshenv").read_text()

        assert copilot_app_tracing.disable()
        # ug created ~/.profile only for its block; ~/.zshenv was the user's.
        assert not (home / ".profile").exists()
        assert (home / ".zshenv").read_text() == "export A=1\n"
        assert not copilot_app_tracing.disable()

    def test_a_shell_no_longer_used_loses_its_block(self, home, monkeypatch):
        shells = ["zsh"]
        monkeypatch.setattr(shell_profile_env, "login_shells", lambda: shells)
        copilot_app_tracing.enable()
        shells[:] = ["fish"]

        copilot_app_tracing.enable()

        assert not (home / ".zshenv").exists()
        assert shell_profile_env.profile_file("fish").is_file()

    def test_a_failed_profile_write_still_leaves_revert_the_written_blocks(self, home, monkeypatch):
        monkeypatch.setattr(shell_profile_env, "login_shells", lambda: ["bash", "zsh"])
        write = shell_profile_env.write
        calls = []

        def fail_second(path, env):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return write(path, env)

        monkeypatch.setattr(shell_profile_env, "write", fail_second)
        with pytest.raises(OSError):
            copilot_app_tracing.enable()
        assert shell_profile_env.BEGIN in calls[0].read_text()

        monkeypatch.setattr(shell_profile_env, "write", write)
        assert copilot_app_tracing.disable()
        assert not calls[0].exists()

    def test_an_unprobed_login_shell_warns_and_writes_nothing(self, home, monkeypatch, capsys):
        monkeypatch.setattr(shell_profile_env, "login_shells", lambda: ["ksh"])

        assert not copilot_app_tracing.enable()

        out = " ".join(capsys.readouterr().out.split())
        assert "doesn't read the profile of your login shell (ksh)" in out
        assert not home.exists() or not any(home.iterdir())

    def test_windows_uses_the_user_environment(self, monkeypatch):
        calls = []
        monkeypatch.setattr(copilot_app_tracing, "current_os", lambda: OS.WINDOWS)
        monkeypatch.setattr(
            windows_user_env,
            "set_values",
            lambda values: calls.append(("set", values)) or (True, []),
        )
        monkeypatch.setattr(
            windows_user_env,
            "remove_values",
            lambda values: calls.append(("remove", values)) or True,
        )

        assert copilot_app_tracing.enable()
        assert copilot_app_tracing.disable()

        assert calls == [
            ("set", copilot_app_tracing.app_env()),
            ("remove", copilot_app_tracing.app_env()),
        ]


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeWinreg(SimpleNamespace):
    def __init__(self, values: dict):
        super().__init__(
            HKEY_CURRENT_USER=1, KEY_QUERY_VALUE=1, KEY_SET_VALUE=2, REG_SZ=1, values=values
        )

    def OpenKey(self, *args):  # noqa: N802 - winreg's naming
        return _FakeKey()

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        del self.values[name]


class TestWindowsUserEnv:
    @pytest.fixture
    def registry(self, monkeypatch):
        values = {"COPILOT_OTEL_ENABLED": "false", "PATH": "C:\\bin"}
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(values))
        broadcasts = []
        monkeypatch.setattr(windows_user_env, "_broadcast", lambda: broadcasts.append(1))
        return values, broadcasts

    def test_sets_only_names_the_user_hasnt_set_and_removes_only_ugs_values(self, registry):
        values, broadcasts = registry
        env = {"COPILOT_OTEL_ENABLED": "true", "COPILOT_OTEL_FILE_EXPORTER_PATH": "C:\\s.jsonl"}

        assert windows_user_env.set_values(env) == (True, ["COPILOT_OTEL_ENABLED"])
        assert windows_user_env.set_values(env) == (False, ["COPILOT_OTEL_ENABLED"])
        assert values["COPILOT_OTEL_FILE_EXPORTER_PATH"] == "C:\\s.jsonl"
        assert values["COPILOT_OTEL_ENABLED"] == "false"

        assert windows_user_env.remove_values(env)
        assert values == {"COPILOT_OTEL_ENABLED": "false", "PATH": "C:\\bin"}
        assert not windows_user_env.remove_values(env)
        assert broadcasts == [1, 1]
