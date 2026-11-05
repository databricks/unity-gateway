"""Real native picker journey; no inference or gateway credentials."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.utils.terminal import AgentTerminal
from ucode.smart_routing import config, session_env, v2


@pytest.mark.parametrize(
    ("session_only", "managed_picker", "feedback", "same_model"),
    [
        (False, False, False, False),
        (True, True, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (False, True, True, False),
        (True, True, False, True),
        (True, True, True, False),
    ],
    ids=[
        "native-saved",
        "native-session-only",
        "managed-picker",
        "feedback-command",
        "feedback-picker",
        "same-model-session-only",
        "feedback-session-only",
    ],
)
def test_auto_picker_tui(
    native_binary, tmp_path, session_only, managed_picker, feedback, same_model
):
    """Scenario: start off, cancel entry, enter Auto, then return to concrete mode.

    Expected: actual native picks change live and saved activity; Escape does not.
    This uses the packaged mod with dummy auth and deliberately sends no inference.
    """
    target = "sonnet" if same_model else "haiku"
    target_model = f"system.ai.claude-{target}-5-5"
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
    source = {
        "UCODE_AUTO_PICKER_CONFIG": config.SUBAGENT_ORCH_AUTO_PICKER_V0_CLAUDE_ONLY,
        "EXPOSE_AUTO_IN_MODEL_PICKER": "1",
        **dict.fromkeys(config.SMART_ROUTING_ENV_KEYS, "0"),
    }
    controls = session_env.start_session(source)
    source["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    options = {
        **v2._claude_mod_launch_options(source),
        "autoMainModel": "system.ai.claude-sonnet-5-5",
    }
    if feedback:
        source["ISAAC_SMART_ROUTER_FEEDBACK"] = "1"
        # Isolate feedback transport; model selection and the session writer stay real.
        executable = tmp_path / "feedback-fixture"
        executable.write_text("""#!/bin/sh
printf '{"message_id":null}\\n'
""")
        executable.chmod(0o700)
        options.update(
            isaacExecutable=str(executable), isaacFeedbackDirectory=str(tmp_path / "feedback")
        )
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
                {"model": "system.ai.claude-sonnet-5-5", "label": "Managed Sonnet"},
                {"model": "system.ai.claude-haiku-5-5", "label": "Managed Haiku"},
            ],
        }
        settings["availableModels"] = [
            "auto",
            "system.ai.claude-sonnet-5-5",
            "system.ai.claude-haiku-5-5",
        ]
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
        "system.ai.claude-sonnet-5-5",
        "--plugin-dir",
        str(plugin),
        "--settings",
        json.dumps(settings),
        *v2._claude_mod_launch_args(options),
    ]
    try:
        with AgentTerminal(session, "claude", command, "auto-picker") as tui:

            def selected_model():
                menu = tui.visible.split("Select model")[-1]
                return next(
                    (line.strip() for line in menu.splitlines() if line.lstrip().startswith("❯")),
                    "",
                )

            tui.boot(timeout=45)
            tui.wait_for(
                lambda text: (
                    "Do you want to use this API key?" in text or "smart router OFF" in text
                ),
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
            tui.submit("/model system.ai.claude-haiku-5-5")
            tui.wait_for(
                lambda _: (
                    json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "0")
                    == "0"
                ),
                "concrete selection disables routing",
                timeout=15,
            )
            assert (
                json.loads(controls.read_text()).get("ENABLE_SMART_ROUTER_ORCHESTRATOR", "0") == "0"
            )
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
                while "Auto" not in selected_model():
                    current = selected_model()
                    assert current not in visited, f"Auto row unavailable:\n{tui.visible}"
                    visited.add(current)
                    tui.send("\x1b[B", "move towards Auto")
                    tui.wait_for(
                        lambda _, before=current: selected_model() != before,
                        "next model option",
                    )
                tui.send("s" if session_only else "\r", "choose Auto")
            tui.wait_for(
                lambda _: (
                    json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "0")
                    == "1"
                ),
                "Auto reenabled",
                timeout=15,
            )
            assert json.loads(controls.read_text())["ENABLE_SMART_ROUTER_ORCHESTRATOR"] == "1"
            assert "Select model" not in tui.visible
            tui.submit("/auto-probe")
            tui.wait_for(
                lambda text: '"model":"system.ai.claude-sonnet-5-5"' in text,
                "Auto uses admin default instead of previous Haiku",
            )
            tui.wait_for(
                lambda text: "unity gateway smart router" in text and "routing subagents" in text,
                "Auto routing banner restored",
            )
            if managed_picker and not feedback:
                active = controls.read_text()
                tui.submit("/model")
                tui.wait_for(lambda text: "Select model" in text, "picker while Auto is active")
                tui.send("\x1b", "cancel leaving Auto")
                tui.wait_for(lambda text: "Select model" not in text, "active picker cancelled")
                assert controls.read_text() == active
            tui.submit("/model" if managed_picker else f"/model {target_model}")
            if feedback:
                tui.wait_for(
                    lambda text: "Why are you leaving Auto?" in text, "Leave Auto feedback"
                )
                assert json.loads(controls.read_text())["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] == "1"
                tui.send("short", "enter insufficient feedback")
                tui.send("\r", "submit insufficient feedback")
                tui.wait_for(
                    lambda text: "at least 16 characters" in text, "minimum feedback length"
                )
                assert json.loads(controls.read_text())["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] == "1"
                tui.send("\x15", "clear short feedback")
                tui.send("I want to choose my own model", "enter feedback")
                tui.send("\r", "submit feedback")
            if managed_picker:
                tui.wait_for(lambda text: "Select model" in text, "picker for leaving Auto")
                visited = set()
                while target not in selected_model().lower():
                    current = selected_model()
                    assert current not in visited, tui.visible
                    visited.add(current)
                    tui.send("\x1b[B", "move towards concrete model")
                    tui.wait_for(
                        lambda _, before=current: selected_model() != before, "next option"
                    )
                tui.send("s" if session_only else "\r", "choose concrete model")
            tui.wait_for(
                lambda _: (
                    json.loads(controls.read_text()).get("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "0")
                    == "0"
                ),
                "native concrete choice disables Auto",
                timeout=15,
            )
            tui.submit("/auto-probe")
            tui.wait_for(
                lambda text: (
                    f'"model":"{target_model}","subagent":"0","orchestrator":"0"' in text
                    and "DISABLED_AUTO" in text
                ),
                "concrete model, disabled activity, and Auto opt-out attribution",
            )
            assert '"model":"auto"' not in tui.visible
            tui.wait_for(lambda text: "smart router OFF" in text, "concrete routing state")
            assert "Feedback is being prepared" not in tui.visible
            assert "Dispatch is paused" not in tui.visible
            tui.submit("/exit")
            tui.finish(timeout=10)
    finally:
        controls.unlink(missing_ok=True)
        controls.parent.rmdir()
