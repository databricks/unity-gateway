"""Real terminal/profile checks; no gateway inference or model responses."""

import json
import os
import re
import sys
from types import SimpleNamespace

import pytest

from tests.integration.utils.terminal import AgentTerminal
from ucode.agents.claude_auto import add_picker_entry, picker_config
from ucode.databricks import AnthropicModelCatalog
from ucode.smart_routing.v2 import _claude_mod_launch_options, _write_routed_claude_plugin


@pytest.mark.parametrize(
    ("alternate", "alternate_label", "context_window"),
    [
        ("claude-haiku-4-5", "Haiku 4.5", 200_000),
        ("claude-sonnet-5-5", "Sonnet 5.5", 1_000_000),
        ("claude-fable-5-1", "Fable 5.1", 1_000_000),
        ("system.ai.gpt-6-astra[1m]", "Astra 6", 1_000_000),
    ],
)
def test_auto_switch_updates_native_profile_without_changing_another_session(
    native_binary, tmp_path, alternate, alternate_label, context_window
):
    """Scenario: change Auto's main model through UG's actual pane in one of two TUIs.

    Expected: one checked Auto row, updated native context and effort, the other
    session unchanged, cancellation preserved, and manual selection disables routing.
    This exercises local model/UI metadata, not inference or an actual /compact call.
    """
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    for directory in (home, cwd):
        directory.mkdir()
    plugin = tmp_path / "plugin"
    _write_routed_claude_plugin(plugin, [])
    observer = tmp_path / "observer"
    (observer / ".claude-plugin").mkdir(parents=True)
    (observer / "hooks").mkdir()
    (observer / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "auto-observer", "version": "1.0.0"})
    )
    (observer / "hooks/hooks.json").write_text('{"modules":["./register.ts"]}')
    (observer / "hooks/register.ts").write_text("""
export function register(on) {
  on('session.start', async ($, e, next) => {
    await $.command.register({name:'auto-observe', description:'Observe test state', immediate:true});
    return next(e);
  });
  on('command.run', {command:'auto-observe'}, async ($, e) => {
    await $.fs.write('report-' + e.args + '.json', JSON.stringify({
      model:await $.session.model(), usage:await $.session.usage(),
      custom:await $.env.get('ANTHROPIC_CUSTOM_MODEL_OPTION'),
      routing:await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY')
    }));
    return {text:'OBSERVED_' + e.args};
  });
}
""")
    models = ["claude-opus-5-5", alternate]
    config = add_picker_entry(
        {
            "apiKeyHelper": "printf fixture-dummy-key",
            "availableModels": models.copy(),
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [
                    {"model": models[0], "label": "Opus 5.5"},
                    {"model": models[1], "label": alternate_label},
                ],
            },
        }
    )
    catalog = json.loads(
        picker_config(
            AnthropicModelCatalog(models, {alternate: alternate_label}), "https://fixture", config
        )
    )
    routing = {
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
    }
    options = {**_claude_mod_launch_options(routing), "autoPicker": json.dumps(catalog)}
    config["pluginConfigs"] = {"ug-smart-router": {"options": options}}
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(config))

    def launch(name):
        (tmp_path / (name + "-controls.json")).write_text("{}")
        env = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "SYSTEMROOT", "TEMP", "TMP")
            if key in os.environ
        }
        env.update(
            routing,
            ANTHROPIC_CUSTOM_MODEL_OPTION=catalog["model"],
            ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Auto",
            ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION=catalog["description"],
            HOME=str(home),
            CLAUDE_CONFIG_DIR=str(home / ".claude"),
            ANTHROPIC_BASE_URL="http://127.0.0.1:1",
            ANTHROPIC_API_KEY="",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CODE_MAX_RETRIES="0",
            CLAUDE_CODE_ELEGANT_MEADOW="1",
            CLAUDE_CODE_USE_GATEWAY="1",
            DISABLE_AUTOUPDATER="1",
            UCODE_SMART_ROUTER_PYTHON=sys.executable,
            UCODE_SESSION_ENV_FILE=str(tmp_path / (name + "-controls.json")),
        )

        def record(filename, data):
            (tmp_path / (name + "-" + filename)).write_text(json.dumps(data, indent=2))

        session = SimpleNamespace(home=home, cwd=cwd, env=env, record=record)
        return AgentTerminal(
            session,
            "claude",
            [
                native_binary,
                "--model",
                catalog["model"],
                "--setting-sources",
                "user",
                "--settings",
                str(settings),
                "--plugin-dir",
                str(plugin),
                "--plugin-dir",
                str(observer),
            ],
            name,
            dimensions=(50, 110),
        )

    def observe(tui, name):
        tui.submit("/auto-observe " + name)
        path = cwd / ("report-" + name + ".json")
        tui.wait_for(lambda _: path.exists(), "the native model profile")
        return json.loads(path.read_text())

    def picker(tui, checked=True):
        tui.submit("/model")
        tui.wait_for(lambda s: "Select model" in s, "the stock model picker")
        rows = [line for line in tui.visible.splitlines() if re.search(r"\d+\. Auto\b", line)]
        assert len(rows) == 1, tui.visible
        assert ("✔" in rows[0]) is checked, tui.visible
        screen = tui.visible
        tui.send("\x1b", "close the model picker")
        tui.wait_for(lambda s: "Select model" not in s, "the closed picker")
        return screen

    selector = "Choose your main model"
    with launch("a") as a, launch("b") as b:
        a.boot(timeout=45)
        b.boot(timeout=45)
        before = observe(a, "before")
        assert before["usage"]["context"]["window"] == 1_000_000
        picker(a)
        a.submit("/auto")
        a.wait_for(lambda s: selector in s, "UG's main-model selector")
        a.send("\x1b[B\r", "select " + alternate_label)
        a.wait_for(
            lambda s: selector not in s and f"Auto({alternate_label}) saved" in s,
            "the native model switch",
        )
        changed = observe(a, "changed")
        assert changed["model"] == catalog["models"][1]["autoModel"]
        assert changed["custom"] == changed["model"]
        assert changed["usage"]["context"]["window"] == context_window
        assert changed["routing"] == "1"
        auto_screen = picker(a)
        if alternate == "claude-haiku-4-5":
            assert "Effort not supported for Auto" in auto_screen
        other = observe(b, "other")
        assert other["model"] == before["model"]
        assert other["usage"]["context"]["window"] == 1_000_000
        assert other["custom"] == before["custom"]
        picker(b)
        # Reselecting Auto must still open the next step; Escape preserves the model.
        a.submit("/model")
        a.choose("Select model", "Auto")
        a.wait_for(lambda s: selector in s, "the selector after Auto reselection")
        a.send("\x1b", "cancel the selector")
        a.wait_for(lambda s: selector not in s, "the dismissed selector")
        assert observe(a, "cancelled")["model"] == changed["model"]
        a.submit("/model " + models[0])
        a.wait_for(lambda s: "Set model to" in s and selector not in s, "the ordinary model switch")
        manual = observe(a, "manual")
        assert manual["model"] == models[0]
        assert manual["routing"] == "0"
        picker(a, checked=False)
        a.submit("/auto")
        a.wait_for(lambda s: selector in s, "the preference-only selector")
        a.send("\x1b[A\r", "choose Opus without enabling Auto")
        a.wait_for(
            lambda s: selector not in s and "Auto(Opus 5.5) saved" in s, "the saved preference"
        )
        preference = observe(a, "preference")
        assert preference["model"] == models[0]
        assert preference["routing"] == "0"
        assert preference["custom"] == catalog["models"][0]["autoModel"]
        a.exit_normally()
        b.exit_normally()
