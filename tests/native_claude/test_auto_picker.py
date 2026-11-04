"""Real native picker journey; no inference or gateway credentials."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.utils.terminal import AgentTerminal
from ucode.smart_routing import config, session_env, v2


@pytest.mark.parametrize("session_only", [False, True])
def test_auto_picker_tui(native_binary, tmp_path, session_only):
    """Scenario: leave Auto, cancel entry, then re-enter on the current model.

    Expected: actual native picks change live and saved activity; Escape does not.
    This uses the packaged mod with dummy auth and deliberately sends no inference.
    """
    mode_prompt = "Choose a mode, then confirm your main model with Enter."
    plugin = tmp_path / "plugin"
    v2._write_routed_claude_plugin(plugin, [])
    (plugin / "hooks/observe.ts").write_text("""
import {register as router} from './register.ts';
export const register = (on, options) => {
  router(on, options);
  on('session.start', {isInteractive: true}, async ($, e, next) => {
    await $.command.register({name:'auto-probe', description:'Test state', immediate:true});
    return next(e);
  });
  on('command.run', {command:'auto-probe'}, async ($) => ({text: JSON.stringify({
    model: await $.session.model(),
    subagent: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
    orchestrator: await $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR'),
    recipe: JSON.parse(await $.env.get('CLAUDE_CODE_EXTRA_BODY') || '{}').smart_router_recipe_name,
  })}));
};
""")
    (plugin / "hooks/hooks.json").write_text('{"modules":["./observe.ts"]}')
    source = config.resolve_environment(
        {"SMART_ROUTER_CONFIG_VERSION": config.SUBAGENT_ORCH_AUTO_PICKER_V0_CLAUDE_ONLY},
        agent="claude",
    )
    controls = session_env.start_session(source)
    source["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    options = v2._claude_mod_launch_options(source)
    env = {key: os.environ[key] for key in ("PATH", "TMP", "TEMP") if key in os.environ}
    env.update(
        HOME=str(tmp_path),
        CLAUDE_CONFIG_DIR=str(tmp_path / "config"),
        ANTHROPIC_BASE_URL="http://127.0.0.1:1",
        ANTHROPIC_API_KEY="fixture-dummy-key",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CODE_MAX_RETRIES="0",
        CLAUDE_CODE_ELEGANT_MEADOW="1",
        DISABLE_AUTOUPDATER="1",
    )
    settings = {
        "env": source,
        "pluginConfigs": {v2.CLAUDE_ROUTING_PLUGIN_NAME: {"options": options}},
    }
    session = SimpleNamespace(
        env=env,
        cwd=tmp_path,
        home=tmp_path,
        record=lambda name, data: (tmp_path / "terminal.json").write_text(json.dumps(data)),
    )
    command = [
        native_binary,
        "--setting-sources",
        "user",
        "--model",
        "claude-sonnet-4-6",
        "--plugin-dir",
        str(plugin),
        "--settings",
        json.dumps(settings),
        *v2._claude_mod_launch_args(options),
    ]
    try:
        with AgentTerminal(session, "claude", command, "auto-picker") as tui:
            tui.boot(timeout=45)
            tui.wait_for(
                lambda text: "Do you want to use this API key?" in text or "for shortcuts" in text,
                "API key approval",
                timeout=15,
            )
            if "Do you want to use this API key?" in tui.visible:
                tui.send("\x1b[A", "select fixture API key")
                tui.send("\r", "confirm fixture API key")
            tui.boot(timeout=30)
            tui.submit("/model haiku")
            tui.wait_for(
                lambda _: (
                    json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY")
                    == "0"
                ),
                "concrete selection disables routing",
                timeout=15,
            )
            assert json.loads(controls.read_text())["ENABLE_SMART_ROUTER_ORCHESTRATOR"] == "0"
            tui.submit("/auto-probe")
            tui.wait_for(lambda text: '"recipe":"DISABLED_AUTO"' in text, "concrete recipe marker")
            off = controls.read_text()
            tui.submit("/model")
            tui.wait_for(lambda text: mode_prompt in text, "Auto popup")
            assert "automatically route subagents" in tui.visible
            tui.send("\x1b", "cancel mode popup")
            tui.wait_for(lambda text: mode_prompt not in text, "mode popup cancelled")
            assert controls.read_text() == off
            tui.submit("/model auto")
            tui.wait_for(lambda text: "Select model" in text, "native main-model picker")
            tui.send("\x1b", "cancel Auto selection")
            tui.wait_for(lambda text: "Select model" not in text, "picker cancelled")
            assert controls.read_text() == off
            tui.submit("/model")
            tui.wait_for(lambda text: mode_prompt in text, "Auto popup again")
            tui.send("\r", "choose Auto")
            tui.wait_for(lambda text: "Select model" in text, "native picker again")
            tui.send("\r", "confirm current main model")
            tui.wait_for(
                lambda _: (
                    json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY")
                    == "1"
                ),
                "Auto reenabled",
                timeout=15,
            )
            assert json.loads(controls.read_text())["ENABLE_SMART_ROUTER_ORCHESTRATOR"] == "1"
            tui.submit("/auto-probe")
            tui.wait_for(
                lambda text: '"recipe":"stable"' in text, "Auto restores the enabled recipe"
            )
            tui.wait_for(
                lambda text: "unity gateway smart router" in text and "routing subagents" in text,
                "Auto routing banner restored",
            )
            tui.submit("/model")
            tui.wait_for(lambda text: mode_prompt in text, "model mode popup")
            tui.send("\t", "focus concrete mode")
            tui.send("\r", "choose concrete mode")
            tui.wait_for(lambda text: "Select model" in text, "native concrete picker")
            tui.send("\r", "confirm concrete main model")
            tui.wait_for(
                lambda _: (
                    json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY")
                    == "0"
                ),
                "native concrete choice disables Auto",
                timeout=15,
            )
            tui.submit("/auto-probe")
            tui.wait_for(lambda text: '"recipe":"DISABLED_AUTO"' in text, "popup concrete recipe")
            if session_only:
                tui.submit("/model haiku")
                tui.wait_for(
                    lambda _: (
                        json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY")
                        == "0"
                    ),
                    "same concrete model disables Auto",
                )
                tui.submit("/model auto")
                tui.wait_for(lambda text: "Select model" in text, "session-only native picker")
                tui.send("\x1b[B", "focus model list")
                tui.send("s", "confirm session-only model")
                tui.wait_for(
                    lambda _: (
                        json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY")
                        == "1"
                    ),
                    "session-only pick enables Auto",
                    timeout=15,
                )
            tui.submit("/exit")
            tui.finish(timeout=10)
    finally:
        controls.unlink(missing_ok=True)
        controls.parent.rmdir()
