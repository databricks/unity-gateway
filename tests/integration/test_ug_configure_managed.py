"""CUJs: configure against a managed workspace, where an admin publishes the setup.

These run against the managed e2e workspace (`E2E_ADMIN_WORKSPACE`) with the checked-in
`managed_workspace_default.json` CodingAgentConfig injected via UCODE_MANAGED_CONFIG_STUB, so they
do not depend on what the workspace happens to publish. `ug configure` applies the config to every
enabled agent without the personal agent selector, and each agent's generated config exposes
exactly the config's static `model_services` (Claude's `availableModels`/`modelPicker`, Codex's
model catalog). The expected model ids mirror that fixture; update both together.
"""

import json
import tomllib
from pathlib import Path

import pytest
from utils.managed import use_managed_config_fixture
from utils.terminal import AgentTerminal

MANAGED_CLAUDE_MODELS = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
MANAGED_CODEX_MODEL = "system.ai.gpt-5-6-sol"


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_ug_configure_managed_claude(live_session, workspace):
    """Scenario: run `ug configure` under an injected managed config.

    Expected: ug applies the admin config to every enabled agent without showing the
    personal agent selector, Claude's generated settings expose exactly the admin's static
    model_services as its picker allow-list, and launching Claude reaches a real gateway
    prompt rather than the account-login flow.
    """
    session = live_session
    use_managed_config_fixture(session, "managed_workspace_default")
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    assert settings.get("availableModels") == MANAGED_CLAUDE_MODELS, settings
    options = (settings.get("modelPicker") or {}).get("options", [])
    assert [option.get("model") for option in options] == MANAGED_CLAUDE_MODELS, settings

    # Drive the real agent: with the managed models in place it must reach a usable
    # gateway prompt, not Claude's own login flow (boot asserts the latter never appears).
    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "managed-claude") as tui:
        tui.boot()
        tui.check_input_and_exit()


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_ug_configure_managed_codex(live_session, workspace):
    """Scenario: run `ug configure` under an injected managed config.

    Expected: ug applies the admin config to every enabled agent without showing the
    personal agent selector, Codex's generated model catalog lists exactly the admin's static
    model_services, the shared Codex App config points at that stable catalog, and configure
    reports the daemon restart step on stderr. A fresh bare
    Codex app-server returns the expected visible model, while the existing TUI assertion reaches
    a prompt, accepts input, and exits normally. GUI rendering and inference are not covered.
    """
    session = live_session
    use_managed_config_fixture(session, "managed_workspace_default")
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    assert "codex app-server daemon restart" in " ".join(result.stderr.split()), result.stderr

    catalog = json.loads((session.home / ".ucode" / "codex-model-catalog.json").read_text())
    listed = [
        model.get("slug")
        for model in catalog.get("models", [])
        if model.get("visibility") == "list"
    ]
    assert listed == [MANAGED_CODEX_MODEL], catalog

    shared_config = tomllib.loads((session.home / ".codex" / "config.toml").read_text())
    assert shared_config.get("model_catalog_json") == str(
        session.home / ".ucode" / "codex-model-catalog.json"
    ), shared_config
    bare_models = session.codex_model_ids(
        ["app-server", "--listen", "stdio://"],
        name="bare-managed-codex-models",
        binary=session.which("codex"),
    )
    assert bare_models == [MANAGED_CODEX_MODEL], bare_models

    with AgentTerminal(session, "codex", [str(session.binary), "codex"], "managed-codex") as tui:
        tui.boot()
        tui.check_input_and_exit()


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_ug_configure_managed_is_idempotent(live_session, workspace):
    """Scenario: run the managed `ug configure` twice in the same session.

    Expected: each run, with no personal agent selector, applies the admin config to both enabled
    agents identically, so a repeat configure neither duplicates, drops, nor rewrites any entry.
    """
    session = live_session
    use_managed_config_fixture(session, "managed_workspace_default")
    runs = []
    for _ in range(2):
        result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
        assert "Select coding agents to configure:" not in result.stdout, result.stdout
        settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
        catalog = json.loads((session.home / ".ucode" / "codex-model-catalog.json").read_text())
        picker = [o.get("model") for o in (settings.get("modelPicker") or {}).get("options", [])]
        listed = [m.get("slug") for m in catalog.get("models", []) if m.get("visibility") == "list"]
        runs.append((settings.get("availableModels"), picker, listed))

    expected = (MANAGED_CLAUDE_MODELS, MANAGED_CLAUDE_MODELS, [MANAGED_CODEX_MODEL])
    assert runs == [expected, expected], runs


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_ug_managed_config_launch_replays_saved_config(live_session, workspace):
    """Scenario: after a managed `ug configure`, launch Claude twice.

    Expected: configure installs a readable machine policy and records a published outcome with no
    expiry stamp. Launches replay that saved config without re-reading it or rewriting either file,
    however much time passes (an admin's later change is picked up by re-running `ug configure`).
    """
    session = live_session
    use_managed_config_fixture(session, "managed_workspace_default")
    policy = Path("/etc/unitygateway/managed-settings.json")
    refresh = session.home / ".ucode" / "refresh_ts.json"
    workspace_key = workspace.rstrip("/")

    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    installed = policy.read_bytes()
    installed_stat = policy.stat()
    assert installed_stat.st_uid == 0
    assert installed_stat.st_mode & 0o777 == 0o644
    assert policy.parent.stat().st_uid == 0
    assert policy.parent.stat().st_mode & 0o777 == 0o755
    assert set(json.loads(installed)) == {"workspace", "config"}
    saved = json.loads(refresh.read_text())[workspace_key]
    assert saved["outcome"] == "published", saved
    assert "last_checked_at" not in saved, saved
    assert refresh.stat().st_mode & 0o777 == 0o600
    # Backdate the record as an older ug would have stamped it: the old five-minute timer would
    # refetch and rewrite it, while a replay leaves it byte-for-byte unchanged.
    records = json.loads(refresh.read_text())
    records[workspace_key]["last_checked_at"] = "2000-01-01T00:00:00+00:00"
    refresh.write_text(json.dumps(records))
    refresh_text = refresh.read_text()

    for label in ("replay-1", "replay-2"):
        with AgentTerminal(session, "claude", [str(session.binary), "claude"], label) as tui:
            tui.boot()
            tui.check_input_and_exit()
        assert refresh.read_text() == refresh_text
        assert policy.read_bytes() == installed
        assert policy.stat().st_ino == installed_stat.st_ino
        assert policy.stat().st_mtime_ns == installed_stat.st_mtime_ns
        assert policy.stat().st_ctime_ns == installed_stat.st_ctime_ns


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_ug_managed_machine_refuses_foreign_workspace_launch(live_session, workspace):
    """Scenario: configure a managed machine, then select a foreign workspace for launch.

    Expected: bare ug, Claude, and Codex refuse the foreign workspace even with skip-preflight
    or dry-run, without changing user state, refresh metadata, or the installed policy.
    """
    session = live_session
    use_managed_config_fixture(session, "managed_workspace_default")
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    policy = Path("/etc/unitygateway/managed-settings.json")
    refresh = session.home / ".ucode" / "refresh_ts.json"
    state = session.home / ".ucode" / "state.json"
    before = {path: path.read_bytes() for path in (policy, refresh, state)}
    installed_stat = policy.stat()
    foreign = "https://not-the-managed-workspace.invalid"
    for tool in (None, "claude", "codex"):
        for dry_run in (False, True):
            args = [tool] if tool else []
            args += ["--workspace", foreign, "--skip-preflight"]
            if dry_run:
                args = ["--dry-run", *args]
            result = session.run(*args, ok=False, timeout=30)
            assert result.returncode == 1, (result.stdout, result.stderr)
            output = " ".join((result.stdout + result.stderr).split())
            assert "Launch refused" in output, output
            assert workspace.rstrip("/") in output, output
            assert foreign in output, output
            assert "IT team or an administrator" in output, output
            assert {path: path.read_bytes() for path in before} == before
            after_stat = policy.stat()
            assert (after_stat.st_ino, after_stat.st_mtime_ns, after_stat.st_ctime_ns) == (
                installed_stat.st_ino,
                installed_stat.st_mtime_ns,
                installed_stat.st_ctime_ns,
            )
