"""Black-box lifecycle coverage for the Codex desktop app's shared config and model catalog."""

import hashlib
import json
import tomllib

import pytest
from utils.managed import use_managed_config_fixture
from utils.terminal import TerminalProcess

pytestmark = [pytest.mark.managed_fixture, pytest.mark.codex]

USER_SETTINGS = "# user-owned Codex settings\n[notice]\nhide_rate_limit_model_nudge = true\n"
RESTART_NOTICE = "codex app-server daemon restart"


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_ug_configure_codex_desktop_repeat_configure(live_session, workspace):
    """Scenario: configure managed Codex twice beside user-owned settings.

    Expected: the shared `~/.codex/config.toml` that the desktop app reads gains one pointer to
    ug's stable catalog and keeps the user's settings; a repeat configure rewrites neither file
    and prints no daemon-restart notice. The catalog's models and a bare `codex app-server`
    `model/list` are covered by `test_ug_configure_managed_codex`, and revert by
    `test_case_02_managed_codex_uses_admin_discovery_after_configure`. Desktop tracing, GUI
    rendering, and inference are not covered.
    """
    session = live_session
    shared_config = session.home / ".codex" / "config.toml"
    catalog_path = session.home / ".ucode" / "codex-model-catalog.json"
    # Ordinary pre-existing user settings, not manufactured gateway state.
    shared_config.parent.mkdir(parents=True)
    shared_config.write_text(USER_SETTINGS)
    use_managed_config_fixture(session, "managed_workspace_default")
    configure_args = ("configure", "--workspace", workspace, "--skip-upgrade")

    first = session.run(*configure_args, timeout=240)
    assert RESTART_NOTICE in " ".join(first.stderr.split()), first.stderr
    configured = tomllib.loads(shared_config.read_text())
    assert configured.pop("model_catalog_json") == str(catalog_path), configured
    assert configured == tomllib.loads(USER_SETTINGS)

    before = (_digest(shared_config), _digest(catalog_path))
    second = session.run(*configure_args, timeout=240)
    assert RESTART_NOTICE not in " ".join(second.stderr.split()), second.stderr
    assert (_digest(shared_config), _digest(catalog_path)) == before


def test_ug_configure_codex_desktop_keeps_user_catalog(live_session, workspace):
    """Scenario: the user already points the desktop app at their own model catalog.

    Expected: configure warns and leaves the user's pointer and catalog file byte-identical, and
    revert keeps both while deleting the catalog ug wrote beside them.
    """
    session = live_session
    shared_config = session.home / ".codex" / "config.toml"
    user_catalog = session.home / "my-catalog.json"
    # User-owned inputs: a catalog file and the pointer to it. JSON quoting keeps Windows paths
    # valid TOML.
    user_catalog.write_text(json.dumps({"models": []}))
    shared_config.parent.mkdir(parents=True)
    shared_config.write_text(f"model_catalog_json = {json.dumps(str(user_catalog))}\n")
    original = (shared_config.read_text(), user_catalog.read_text())
    use_managed_config_fixture(session, "managed_workspace_default")

    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "custom model catalog" in " ".join(result.stderr.split()), result.stderr
    assert (shared_config.read_text(), user_catalog.read_text()) == original

    with TerminalProcess(session, "ug", [str(session.binary), "revert"], "desktop-keep") as tty:
        tty.finish()
    assert (shared_config.read_text(), user_catalog.read_text()) == original
    assert not list((session.home / ".ucode").glob("codex-model-catalog*.json"))
