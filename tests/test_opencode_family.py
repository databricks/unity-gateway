"""Tests for the code OpenCode and Kilo share (the auth plugin and the provider ids)."""

from __future__ import annotations

import json
import re

import pytest

from ucode.agents import kilo, opencode

WS = "https://example.databricks.com"

STATES = [
    pytest.param({"workspace": WS}, id="plain"),
    pytest.param({"workspace": WS, "profile": "my profile"}, id="profile"),
    pytest.param({"workspace": WS, "profile": "my profile", "use_pat": True}, id="profile-pat"),
]

MANAGED_PROVIDER_IDS = ["databricks-anthropic", "databricks-google", "databricks-oss"]


@pytest.fixture(autouse=True)
def _fixed_ug_binary(monkeypatch):
    monkeypatch.setattr("ucode.databricks.shutil.which", lambda command: f"/opt/{command}")


def _plugin_provider_ids(plugin: str) -> list[str]:
    block = re.search(r"DATABRICKS_PROVIDERS = new Set\(\[(.*?)\]\)", plugin, re.DOTALL)
    assert block is not None
    return re.findall(r'"([^"]+)"', block.group(1))


class TestSharedAuthPlugin:
    @pytest.mark.parametrize(
        ("state", "options"),
        [
            pytest.param({"workspace": WS + "/"}, [], id="plain"),
            pytest.param(
                {"workspace": WS, "profile": "my profile"},
                ["--profile", "my profile"],
                id="profile",
            ),
            pytest.param(
                {"workspace": WS, "profile": "my profile", "use_pat": True},
                ["--profile", "my profile", "--use-pat"],
                id="profile-pat",
            ),
        ],
    )
    def test_plugin_runs_the_token_command_with_force_refresh(self, state, options):
        plugin = opencode.render_auth_plugin(state)

        command = re.search(r"const AUTH_COMMAND = (\[.*?\])\n", plugin).group(1)
        assert json.loads(command) == [
            "/opt/ug",
            "auth-token",
            "--host",
            WS,
            *options,
            "--force-refresh",
        ]

    @pytest.mark.parametrize("state", STATES)
    def test_kilo_plugin_equals_opencode_v1_plugin(self, state):
        kilo_plugin = kilo.render_auth_plugin(state)

        assert kilo_plugin == opencode.render_auth_plugin(state, v2=False)
        assert kilo_plugin == opencode.render_auth_plugin(state)

    def test_opencode_v2_plugin_shares_the_prelude_with_v1(self):
        v1 = opencode.render_auth_plugin({"workspace": WS}, v2=False)
        v2 = opencode.render_auth_plugin({"workspace": WS}, v2=True)

        prelude = v1.split("function requestWithToken", 1)[0]
        assert v2.startswith(prelude)
        assert "export default {" in v2
        assert "export default" not in v1

    @pytest.mark.parametrize(
        ("render", "keys"),
        [
            pytest.param(lambda: opencode.render_auth_plugin({"workspace": WS}), opencode, id="v1"),
            pytest.param(
                lambda: opencode.render_auth_plugin({"workspace": WS}, v2=True),
                opencode,
                id="v2",
            ),
            pytest.param(lambda: kilo.render_auth_plugin({"workspace": WS}), kilo, id="kilo"),
        ],
    )
    def test_plugin_manages_exactly_the_provider_keys(self, render, keys):
        plugin = render()

        assert _plugin_provider_ids(plugin) == MANAGED_PROVIDER_IDS
        assert [path[-1] for path in keys.PROVIDER_KEYS] == MANAGED_PROVIDER_IDS
        assert "__AUTH_COMMAND__" not in plugin
        assert "__PROVIDER_IDS__" not in plugin

    @pytest.mark.parametrize("installed", ["7.3.1", "2.0.14", "9.9.9", "unknown"])
    def test_kilo_writes_the_v1_plugin_for_any_installed_version(
        self, installed, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(kilo, "KILO_CONFIG_PATH", tmp_path / "kilo.json")
        monkeypatch.setattr(kilo, "agent_version", lambda binary: installed)
        state = {"workspace": WS}

        kilo.write_auth_plugin(state)

        written = (tmp_path / "plugin" / kilo.KILO_AUTH_PLUGIN_PATH.name).read_text()
        assert written == kilo.render_auth_plugin(state)
        assert "export const UcodeDatabricksAuth" in written
        assert "export default" not in written
