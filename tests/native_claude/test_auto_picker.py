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


@pytest.mark.parametrize("static", [False, True])
def test_native_auto_picker_journey(native_binary, static, tmp_path):
    """Scenario: select Auto, scroll 23 models, cancel, then select a manual model.

    Expected: a native picker at 60 columns; continuous numbering; Auto restored
    after confirmation or Escape; manual selection disables routing; /auto while
    manual only saves a preference. Uses real Claude and UG's session controls,
    with a fixture catalog and observer plugin. No gateway or inference coverage.
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
    models = ["system.ai.claude-opus-5-5", *[f"system.ai.test-model-{i:02}" for i in range(2, 24)]]
    labels = {model: f"Model {i:02}" for i, model in enumerate(models, 1)}
    config = add_picker_entry(
        {
            "apiKeyHelper": "printf fixture-dummy-key",
            "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": models[0]},
            "availableModels": models.copy(),
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [
                    {"model": model, "label": labels[model], "behavesAs": "claude-opus-5-5"}
                    for model in models
                ],
            },
        },
        static_auto_row=static,
    )
    catalog = json.loads(
        picker_config(AnthropicModelCatalog(models, labels), "https://fixture", config)
    )
    if static:
        catalog["staticModel"] = catalog["model"]
    routing = {
        "SMART_ROUTER_CONFIG_VERSION": "subagent_auto_v0_claude_only",
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
        )

    def observe(tui, name):
        tui.submit("/auto-observe " + name)
        path = cwd / ("report-" + name + ".json")
        tui.wait_for(lambda _: path.exists(), "the native model profile")
        return json.loads(path.read_text())

    with launch("native-large-flow") as tui:
        tui.boot(timeout=45)
        tui.child.child.setwinsize(36, 60)
        tui.screen.resize(lines=36, columns=60)
        tui.submit("/model")
        tui.choose("Select model", "Auto")
        tui.wait_for(
            lambda s: (
                "Select model" in s
                and any(
                    label in tui.selected_line(after="Select model")
                    for label in ("Model 01", "Opus 5.5")
                )
            ),
            "native main picker",
        )
        seen = []
        while "Model 12" not in tui.selected_line(after="Select model"):
            line = tui.selected_line(after="Select model")
            assert line not in seen, tui.visible
            seen.append(line)
            tui.send("\x1b[B", "next native model")
            tui.wait_for(
                lambda s, before=line: tui.selected_line(after="Select model") != before, "next row"
            )
        assert "More models" not in tui.visible
        index = int(re.search(r"(\d+)\.", tui.selected_line(after="Select model"))[1])
        assert index >= 12, tui.visible
        tui.send("\x1b[C", "increase native effort")
        tui.wait_for(lambda s: "High effort" in s, "native inline effort")
        tui.send("\r", "choose model twelve")
        tui.wait_for(
            lambda s: (
                "Select model" not in s
                and "test-model-12, subagents smart routed." in " ".join(s.split())
            ),
            "Auto on model twelve",
        )
        first = observe(tui, "model-twelve")
        assert first["routing"] == "1", first
        assert first["model"] == (
            catalog["model"] if static else catalog["models"][11]["autoModel"]
        ), first
        tui.submit("/effort status")
        tui.wait_for(lambda s: "Current effort level: high" in s, "native effort persisted")
        tui.submit("/auto")
        tui.wait_for(
            lambda s: "Select model" in s and "Model 12" in tui.selected_line(after="Select model"),
            "reopen on main model",
        )
        tui.send("\x1b", "cancel the native picker")
        tui.wait_for(lambda s: "Select model" not in s, "picker closed")
        assert observe(tui, "cancelled")["model"] == first["model"]
        tui.submit("/auto")
        tui.choose("Select model", "Default (recommended)")
        tui.wait_for(lambda s: "Select model" not in s, "default chosen")
        default = observe(tui, "default")
        assert default["model"] == catalog["model"], default
        assert default["routing"] == "1", default
        tui.submit("/model")
        tui.choose("Select model", "Model 03")
        manual = observe(tui, "manual")
        assert manual["model"] == models[2], manual
        assert manual["routing"] == "0", manual
        tui.submit("/auto")
        tui.choose("Select model", "Model 15")
        tui.wait_for(
            lambda s: "preference saved: Model 15." in " ".join(s.split()),
            "saved inactive preference",
        )
        inactive = observe(tui, "inactive")
        assert inactive["model"] == models[2], inactive
        assert inactive["routing"] == "0", inactive
        tui.exit_normally()
