"""Real native picker journey; no inference or gateway credentials."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.utils.terminal import AgentTerminal
from ucode.smart_routing import config, session_env, v2


@pytest.mark.parametrize(
    ("session_only", "managed_picker"),
    [(False, False), (True, True), (False, True)],
    ids=["native-saved", "native-session-only", "managed-picker"],
)
def test_auto_picker_tui(native_binary, tmp_path, session_only, managed_picker):
    """Scenario: start off, cancel entry, enter Auto, then return to concrete mode.

    Expected: actual native picks change live and saved activity; Escape does not.
    This uses the packaged mod with dummy auth and deliberately sends no inference.
    """
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
    body: await $.env.get('CLAUDE_CODE_EXTRA_BODY'),
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
    options = {**v2._claude_mod_launch_options(source), "autoMainModel": "claude-sonnet-4-6"}
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
    if managed_picker:
        settings["modelPicker"] = {
            "replaceBuiltInOptions": True,
            "options": [
                {"model": "auto", "label": "Auto"},
                {"model": "claude-sonnet-4-6", "label": "Managed Sonnet"},
                {"model": "haiku", "label": "Managed Haiku"},
            ],
        }
        settings["availableModels"] = ["auto", "claude-sonnet-4-6", "haiku"]
        settings["enforceAvailableModels"] = True
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
            tui.wait_for(lambda text: "smart router OFF" in text, "fresh session starts off")
            assert "routing subagents" not in tui.visible
            tui.submit("/auto-probe")
            tui.wait_for(
                lambda text: '"subagent":"0","orchestrator":"0"' in text,
                "fresh session activity is off",
            )
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
            off = controls.read_text()
            tui.submit("/model")
            tui.wait_for(lambda text: "Select model" in text, "normal native model picker")
            if managed_picker:
                assert "Auto" in tui.visible
            tui.send("\x1b", "cancel native picker")
            tui.wait_for(lambda text: "Select model" not in text, "native picker cancelled")
            assert controls.read_text() == off
            tui.submit("/model" if managed_picker else "/model auto")
            if managed_picker:
                tui.wait_for(lambda text: "Select model" in text, "normal picker again")
                visited = set()
                while "Auto" not in tui.selected_line():
                    current = tui.selected_line()
                    assert current not in visited, f"Auto row unavailable:\n{tui.visible}"
                    visited.add(current)
                    tui.send("\x1b[B", "move towards Auto")
                    tui.wait_for(
                        lambda _, before=current: tui.selected_line() != before,
                        "next model option",
                    )
                tui.send("s" if session_only else "\r", "choose Auto")
            tui.wait_for(
                lambda _: (
                    json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY")
                    == "1"
                ),
                "Auto reenabled",
                timeout=15,
            )
            assert json.loads(controls.read_text())["ENABLE_SMART_ROUTER_ORCHESTRATOR"] == "1"
            assert "Select model" not in tui.visible
            tui.submit("/auto-probe")
            tui.wait_for(
                lambda text: '"model":"claude-sonnet-4-6"' in text,
                "Auto uses admin default instead of previous Haiku",
            )
            tui.wait_for(
                lambda text: "unity gateway smart router" in text and "routing subagents" in text,
                "Auto routing banner restored",
            )
            tui.submit("/model haiku")
            tui.wait_for(
                lambda _: (
                    json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY")
                    == "0"
                ),
                "native concrete choice disables Auto",
                timeout=15,
            )
            if managed_picker:
                tui.submit("/auto-probe")
                tui.wait_for(
                    lambda text: '"subagent":"0","orchestrator":"0"' in text,
                    "concrete mode after managed selection",
                )
                assert '"model":"auto"' not in tui.visible
            tui.submit("/exit")
            tui.finish(timeout=10)
    finally:
        controls.unlink(missing_ok=True)
        controls.parent.rmdir()
