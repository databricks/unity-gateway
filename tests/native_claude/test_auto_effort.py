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


@pytest.mark.parametrize("dimensions", [(50, 110), (22, 60)], ids=["docked", "narrow-overflow"])
def test_auto_inline_character_effort(native_binary, tmp_path, dimensions):
    """Scenario: choose Auto, then use h/l to adjust effort in the main-model pane.

    Expected: arrows move between models even when the pane overflows; only
    confirmation changes native effort; model, routing and native
    /model remain consistent, and Escape preserves the selected Auto alias.
    This is a real terminal/settings check without inference.
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
    models = ["claude-opus-5-5", "claude-sonnet-5-5"]
    config = add_picker_entry(
        {
            "apiKeyHelper": "printf fixture-dummy-key",
            "availableModels": models.copy(),
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [
                    {"model": models[0], "label": "Opus 5.5"},
                    {"model": models[1], "label": "Sonnet 5.5"},
                ],
            },
        }
    )
    catalog = json.loads(
        picker_config(
            AnthropicModelCatalog(models, {models[1]: "Sonnet 5.5"}), "https://fixture", config
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

    def pane_text(screen):
        # Claude 2.1.290 can use a narrow side pane. Join its wrapped text
        # independently of the main transcript, retaining the full shortcut label.
        return " ".join(
            line.split("│", 1)[1].strip("│ ") for line in screen.splitlines() if "│" in line
        )

    with launch("inline-effort") as tui:
        tui.boot(timeout=45)
        tui.submit("/model")
        tui.choose("Select model", "Auto")
        heading = "Choose your main model"  # The remainder wraps in a narrow native dock.
        if dimensions[1] < 80:
            # The inline pane is only a few rows tall here; model navigation must
            # reveal the focused row rather than scroll past it without selecting.
            tui.wait_for(lambda s: "❯ Opus 5.5" in s, "the initial narrow selection")
            tui.send("\x1b[B", "select Sonnet in the overflowing pane")
            tui.wait_for(lambda s: "❯ Sonnet 5.5" in s, "Sonnet selected by Down")
            tui.send("\x1b[A", "select Opus in the overflowing pane")
            tui.wait_for(lambda s: "❯ Opus 5.5" in s, "Opus selected by Up")
            tui.send("\x1b[B", "return to Sonnet")
            tui.wait_for(lambda s: "❯ Sonnet 5.5" in s, "Sonnet selected again")
            tui.send("\r", "save the arrow-selected model")
            tui.wait_for(lambda s: "Auto (Sonnet 5.5) saved" in s, "saved narrow selection")
            selected = observe(tui, "narrow-selected")
            assert selected["model"] == catalog["models"][1]["autoModel"]
            assert selected["routing"] == "1"
            tui.submit("/auto")
            tui.wait_for(lambda s: "❯ Sonnet 5.5" in s, "reopened narrow selector")
            tui.send("\x1b[A", "draft Opus without saving")
            tui.wait_for(lambda s: "❯ Opus 5.5" in s, "draft Opus selection")
            tui.send("\x1b", "cancel narrow selection")
            tui.wait_for(lambda s: "❯ Opus 5.5" not in s, "closed narrow selector")
            assert observe(tui, "narrow-cancelled")["model"] == selected["model"]
            tui.exit_normally()
            return
        tui.wait_for(
            lambda s: heading in s and "h/l to adjust effort" in pane_text(s),
            "the Auto effort pane",
        )
        tui.send("l", "increase effort without moving model focus")
        tui.wait_for(lambda s: "High effort" in s, "the higher draft effort")
        tui.send("h", "decrease effort without moving model focus")
        tui.wait_for(lambda s: "Medium effort" in s, "the lower draft effort")
        tui.send("l", "draft high effort on Opus")
        tui.wait_for(lambda s: "High effort" in s, "the high Opus draft")
        tui.send("\x1b[B", "move down to Sonnet rather than scrolling the pane")
        tui.wait_for(
            lambda s: "❯ Sonnet 5.5" in s and "Medium effort" in s,
            "Sonnet selected with its own effort",
        )
        tui.send("\x1b[A", "move up to Opus rather than scrolling the pane")
        tui.wait_for(
            lambda s: "❯ Opus 5.5" in s and "High effort" in s,
            "Opus selected with its preserved draft",
        )
        tui.send("\r", "save model with high effort")
        tui.wait_for(
            lambda s: heading not in s and "Auto (Opus 5.5) saved" in s, "saved Auto effort"
        )
        current = observe(tui, "selected")
        assert current["model"] == catalog["models"][0]["autoModel"], current
        tui.submit("/effort status")
        tui.wait_for(lambda s: "Current effort level: high" in s, "native effort agrees")
        picker(tui)
        tui.submit("/auto")
        tui.wait_for(lambda s: heading in s, "reopened effort pane")
        tui.send("h", "draft lower effort")
        tui.wait_for(lambda s: "Medium effort" in s, "lower draft")
        tui.send("\x1b", "cancel the effort change")
        tui.wait_for(lambda s: heading not in s, "cancelled pane")
        tui.submit("/effort status")
        tui.wait_for(lambda s: "Current effort level: high" in s, "cancelled effort unchanged")
        assert observe(tui, "cancelled")["model"] == current["model"]
        tui.exit_normally()
