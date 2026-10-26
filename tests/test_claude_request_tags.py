"""AI_GATEWAY_REQUEST_TAGS reaches Claude per launch and is never written to a settings file."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ucode.agents import claude
from ucode.agents.args import LaunchOptions
from ucode.constants import AI_GATEWAY_REQUEST_TAGS_HEADER

WS = "https://example.cloud.databricks.com"
TAGS = '{"source":"isaac"}'
LINE = f"{AI_GATEWAY_REQUEST_TAGS_HEADER}: {TAGS}"
LOWER_NAME = AI_GATEWAY_REQUEST_TAGS_HEADER.lower()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("UG_HEADERS", raising=False)
    monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)


def test_no_tags_does_nothing():
    assert claude._deliver_request_tags() is None
    assert "UG_HEADERS" not in claude.os.environ


def test_header_mod_carries_tags_via_ug_headers(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
    monkeypatch.setenv("UG_HEADERS", "X-Team: a")
    monkeypatch.setattr(claude, "header_mod_active", lambda: True)
    assert claude._deliver_request_tags() is None
    assert claude.os.environ["UG_HEADERS"] == f"X-Team: a\n{LINE}"


def test_header_mod_keeps_an_explicit_tags_header_in_any_case(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
    explicit = f'{LOWER_NAME}: {{"a":1}}'
    monkeypatch.setenv("UG_HEADERS", explicit)
    monkeypatch.setattr(claude, "header_mod_active", lambda: True)
    claude._deliver_request_tags()
    assert claude.os.environ["UG_HEADERS"] == explicit


def _no_mod(monkeypatch, tmp_path, managed_doc, saved_headers):
    managed = tmp_path / "managed-settings.json"
    if managed_doc is not None:
        managed.write_text(
            managed_doc if isinstance(managed_doc, str) else json.dumps(managed_doc),
            encoding="utf-8",
        )
    saved = tmp_path / "ucode-settings.json"
    saved.write_text(
        json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": saved_headers}}), encoding="utf-8"
    )
    monkeypatch.setattr(claude, "header_mod_active", lambda: False)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed)
    monkeypatch.setattr(
        claude, "read_managed_file", lambda p: p.read_text() if p.exists() else None
    )
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", saved)
    monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
    return managed, saved


def test_without_managed_headers_tags_go_in_a_launch_override(monkeypatch, tmp_path):
    managed, saved = _no_mod(monkeypatch, tmp_path, None, "User-Agent: ug\nx-a: b")
    before = saved.read_text()
    override = claude._deliver_request_tags()
    assert override == {"env": {"ANTHROPIC_CUSTOM_HEADERS": f"User-Agent: ug\nx-a: b\n{LINE}"}}
    assert saved.read_text() == before
    assert not managed.exists()
    assert "UG_HEADERS" not in claude.os.environ


@pytest.mark.parametrize(
    "managed_doc",
    [
        {"env": {"OTHER": "1"}},
        {"permissions": {"deny": ["Read(x)"]}},
        "{not json",
        "[]",
    ],
)
def test_managed_file_without_custom_headers_still_gets_the_override(
    monkeypatch, tmp_path, managed_doc
):
    _no_mod(monkeypatch, tmp_path, managed_doc, "x-a: b")
    warnings = []
    monkeypatch.setattr(claude, "print_warning", warnings.append)
    override = claude._deliver_request_tags()
    assert override == {"env": {"ANTHROPIC_CUSTOM_HEADERS": f"x-a: b\n{LINE}"}}
    assert not warnings


def test_override_replaces_a_stale_saved_tags_line_in_any_case(monkeypatch, tmp_path):
    _no_mod(monkeypatch, tmp_path, None, f"x-a: b\n{LOWER_NAME}: {{}}")
    override = claude._deliver_request_tags()
    assert (override or {})["env"]["ANTHROPIC_CUSTOM_HEADERS"] == f"x-a: b\n{LINE}"


def test_managed_headers_without_mod_warn_and_write_nothing(monkeypatch, tmp_path):
    managed_doc = {"env": {"ANTHROPIC_CUSTOM_HEADERS": "x-a: b"}}
    managed, _ = _no_mod(monkeypatch, tmp_path, managed_doc, "x-a: b")
    before = managed.read_text()
    warnings = []
    monkeypatch.setattr(claude, "print_warning", warnings.append)
    assert claude._deliver_request_tags() is None
    assert len(warnings) == 1
    assert "ANTHROPIC_CUSTOM_HEADERS" in warnings[0]
    assert "ug configure" in warnings[0]
    assert managed.read_text() == before
    assert "UG_HEADERS" not in claude.os.environ


def test_write_tool_config_leaves_both_files_unchanged_by_tags(monkeypatch, claude_managed_env):
    managed_path, _, writes = claude_managed_env()
    state = {"workspace": WS, "codex_models": []}
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    managed = managed_path.read_text()
    user = claude.CLAUDE_SETTINGS_PATH.read_text()
    count = len(writes)

    monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
    claude.write_tool_config(state, "databricks-claude-sonnet-4")

    assert managed_path.read_text() == managed
    assert claude.CLAUDE_SETTINGS_PATH.read_text() == user
    assert len(writes) == count
    assert "Request-Tags" not in managed + user


def _no_managed_config(monkeypatch):
    monkeypatch.setattr(
        claude, "refresh_managed_config", lambda *a, **kw: SimpleNamespace(manifest=None)
    )


def test_configure_strips_tags_an_older_ug_persisted_in_its_private_file(
    monkeypatch, claude_managed_env
):
    claude_managed_env()
    _no_managed_config(monkeypatch)
    claude.CLAUDE_SETTINGS_PATH.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_CUSTOM_HEADERS": f"x-keep: 1\n{AI_GATEWAY_REQUEST_TAGS_HEADER}: old"
                }
            }
        ),
        encoding="utf-8",
    )
    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")
    headers = json.loads(claude.CLAUDE_SETTINGS_PATH.read_text())["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "Request-Tags" not in headers
    assert "x-keep: 1" in headers


def test_configure_keeps_a_tags_line_in_the_os_managed_file(monkeypatch, claude_managed_env):
    isaac = f"{LOWER_NAME}: source=isaac-cli"
    managed_path, _, _ = claude_managed_env({"env": {"ANTHROPIC_CUSTOM_HEADERS": isaac}})
    _no_managed_config(monkeypatch)
    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")
    headers = json.loads(managed_path.read_text())["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert isaac in headers.splitlines()


class TestLaunch:
    @pytest.fixture(autouse=True)
    def _launch_env(self, monkeypatch, tmp_path):
        saved = tmp_path / "ucode-settings.json"
        saved.write_text(
            json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": "x-a: b"}}), encoding="utf-8"
        )
        monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", saved)
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: tmp_path / "none.json")
        monkeypatch.setattr(claude, "header_mod_active", lambda: False)
        monkeypatch.setattr(claude, "get_databricks_token", lambda *_a: "token")
        monkeypatch.setattr(claude, "_resolve_launch_binary", lambda binary: binary)
        monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
        self.calls: list[list[str]] = []
        monkeypatch.setattr(claude, "exec_or_spawn", lambda argv: self.calls.append(argv))

    def _settings(self, argv):
        return json.loads(argv[argv.index("--settings") + 1])

    def test_plain_launch_passes_tags_through_settings(self):
        claude.launch({"workspace": WS}, [], options=LaunchOptions())
        env = self._settings(self.calls[0])["env"]
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == f"x-a: b\n{LINE}"

    def test_tags_merge_with_a_pinned_model(self):
        claude.launch(
            {"workspace": WS},
            [],
            options=LaunchOptions(user_pinned_model="databricks-claude-opus-4"),
        )
        env = self._settings(self.calls[0])["env"]
        assert env["ANTHROPIC_CUSTOM_HEADERS"].endswith(LINE)
        assert env["ANTHROPIC_MODEL"]

    def test_tags_merge_with_the_picker_model(self):
        state = {"workspace": WS, "_claude_launch_picker_models": ["claude-picker"]}
        claude.launch(state, [], options=LaunchOptions())
        settings = self._settings(self.calls[0])
        assert settings["model"] == "claude-picker"
        assert settings["env"]["ANTHROPIC_CUSTOM_HEADERS"].endswith(LINE)

    def test_provider_launch_without_tags_keeps_only_the_picker_override(self, monkeypatch):
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS")
        state = {"workspace": WS, "_claude_launch_picker_models": ["claude-picker"]}
        claude.launch(state, [], options=LaunchOptions())
        settings = self._settings(self.calls[0])
        assert settings["model"] == "claude-picker"
        # Same headers as the saved file: no tags line and no extra override keys appear.
        assert settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "x-a: b"

    def test_launch_without_tags_or_models_hands_over_the_saved_file(self, monkeypatch):
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS")
        claude.launch({"workspace": WS}, [], options=LaunchOptions())
        assert self.calls == [["claude", "--settings", str(claude.CLAUDE_SETTINGS_PATH)]]

    def test_relayed_launch_passes_tags_to_the_relay(self, monkeypatch):
        relayed = Mock()
        monkeypatch.setattr(claude, "_launch_relayed", relayed)
        claude.launch({"workspace": WS, "claude_relayed": True}, [], options=LaunchOptions())
        override = relayed.call_args.kwargs["settings_override"]
        assert override["env"]["ANTHROPIC_CUSTOM_HEADERS"].endswith(LINE)

    def test_relayed_argv_carries_the_override(self):
        argv = claude._build_claude_argv(
            "claude",
            [],
            relayed=True,
            settings_override={"env": {"ANTHROPIC_CUSTOM_HEADERS": LINE}},
        )
        assert self._settings(argv)["env"]["ANTHROPIC_CUSTOM_HEADERS"] == LINE

    def test_smart_routing_composes_the_tags_into_its_settings(self, monkeypatch):
        launch_v2 = Mock()
        monkeypatch.setattr(claude.smart_routing_v2, "launch_claude", launch_v2)
        claude.launch({"workspace": WS}, [], options=LaunchOptions(launch_smart_routing=True))
        settings, _ = launch_v2.call_args.kwargs["compose_settings"]([])
        assert settings["env"]["ANTHROPIC_CUSTOM_HEADERS"].endswith(LINE)
        assert not self.calls

    def test_smart_routing_fallback_keeps_the_tags(self, monkeypatch):
        monkeypatch.setattr(
            claude.smart_routing_v2,
            "launch_claude",
            Mock(side_effect=claude.smart_routing_v2.ClaudeRoutingSetupError("boom")),
        )
        claude.launch({"workspace": WS}, [], options=LaunchOptions(launch_smart_routing=True))
        env = self._settings(self.calls[0])["env"]
        assert env["ANTHROPIC_CUSTOM_HEADERS"].endswith(LINE)
        assert env[claude.smart_routing_v2.ENABLE_SMART_ROUTING_ENV_VAR] == "0"

    def test_managed_headers_without_mod_launch_without_tags(self, monkeypatch, tmp_path):
        managed = tmp_path / "managed.json"
        managed.write_text(json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": "x: y"}}))
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed)
        monkeypatch.setattr(
            claude, "read_managed_file", lambda p: p.read_text() if p.exists() else None
        )
        warnings = []
        monkeypatch.setattr(claude, "print_warning", warnings.append)
        claude.launch({"workspace": WS}, [], options=LaunchOptions())
        assert any("AI_GATEWAY_REQUEST_TAGS" in w for w in warnings)
        assert self.calls[0][-2:] == ["--settings", str(claude.CLAUDE_SETTINGS_PATH)] or (
            "Request-Tags" not in " ".join(self.calls[0])
        )
