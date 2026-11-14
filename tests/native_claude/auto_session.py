"""Real terminal/profile checks; no gateway inference or model responses."""

import json
import os
import re
import sys
from types import SimpleNamespace

from tests.integration.utils.terminal import AgentTerminal
from ucode.agents.claude_auto import add_picker_entry, picker_config
from ucode.databricks import AnthropicModelCatalog
from ucode.smart_routing.v2 import _claude_mod_launch_options, _write_routed_claude_plugin


def auto_session(native_binary, tmp_path, models, labels, dimensions=(50, 110)):
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
    config = add_picker_entry(
        {
            "apiKeyHelper": "printf fixture-dummy-key",
            "availableModels": models.copy(),
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [
                    *[{"model": model, "label": labels[model]} for model in models],
                ],
            },
        }
    )
    catalog = json.loads(
        picker_config(AnthropicModelCatalog(models, labels), "https://fixture", config)
    )
    routing = {
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
        "ENABLE_SMART_ROUTER_AUTO_MODE": "1",
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
            dimensions=dimensions,
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

    return SimpleNamespace(launch=launch, observe=observe, picker=picker, catalog=catalog)
