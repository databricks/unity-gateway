"""CUJ: ug's GitHub Copilot app plugin stays out of Copilot CLI sessions."""

import json
from pathlib import Path

import pytest
from utils.constants import CLAUDE_TEST_MODEL, COPILOT_HEADLESS_FLAGS
from utils.evidence import FileTask, assert_completed_task_model, assert_no_session_registry_left

pytestmark = [pytest.mark.live, pytest.mark.copilot]

# CI has no GitHub Copilot desktop app, and configure skips the app when it finds none, so the
# plugin is installed by the step configure runs once it finds one.
INSTALL_APP_PLUGIN = """
import ucode.agents.desktop.setup as setup
from ucode.state import load_state

setup.configure_copilot_app(load_state(), provider=None, location=None)
"""


def _venv_python(binary: Path) -> Path:
    bin_dir = binary.resolve().parent
    for name in ("python", "python3", "python.exe"):
        if (bin_dir / name).is_file():
            return bin_dir / name
    pytest.fail(f"No Python interpreter next to the installed ug in {bin_dir}")


def _extension_dir(session) -> Path:
    return session.home / ".ucode/copilot-app-plugin/plugins/unity-gateway/extensions"


def test_ug_copilot_session_ignores_the_app_plugin(live_session, workspace, installed_binary):
    """Scenario: configure Copilot, install ug's app plugin into the same Copilot home, turn
    Copilot's experimental features on (which is when the CLI starts plugin extensions), then run
    a headless prompt through `ug copilot`.

    Expected: the task completes on the Databricks model as usual; the plugin's extension ran,
    recorded that it was not under the desktop app, served no rows and synced nothing; the
    session registry is gone afterwards.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    session.run("-c", INSTALL_APP_PLUGIN, binary=_venv_python(installed_binary))
    settings_path = session.home / ".copilot/settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "unity-gateway@databricks-unity-gateway" in settings.get("enabledPlugins", {}), settings
    settings["experimental"] = True
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    task = FileTask(session)

    result = session.run(
        "copilot",
        "--model",
        CLAUDE_TEST_MODEL,
        "-p",
        task.prompt,
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )

    task.assert_headless_answer("copilot", result)
    assert_completed_task_model(session, "copilot", task.value, CLAUDE_TEST_MODEL)
    extension_dir = _extension_dir(session)
    sessions_file = extension_dir / "seat-relay-sessions.json"
    assert sessions_file.is_file(), sorted(path.name for path in extension_dir.iterdir())
    entries = json.loads(sessions_file.read_text(encoding="utf-8"))
    session.record("app-plugin-sessions.json", entries)
    assert entries and all(entry.get("redirect") == "not the app" for entry in entries), entries
    assert not (extension_dir / "app-sync.json").exists(), "the CLI session synced the app's rows"
    assert_no_session_registry_left(session)
