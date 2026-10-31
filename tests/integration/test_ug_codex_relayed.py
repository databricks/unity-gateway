"""CUJs: a relayed (ChatGPT-subscription relay) Codex session, launched and configured through ug."""

import os
import tomllib

import pytest
from utils.constants import CODEX_RELAYED_MODEL
from utils.evidence import FileTask
from utils.terminal import ConfigureTerminal, TerminalProcess

pytestmark = [pytest.mark.live, pytest.mark.codex]

# The Codex app's relay provider, written only by `ug configure` (never by a launch).
APP_RELAY_PROVIDER = "DatabricksRelay"
APP_RELAY_WARNING = "Could not update the Codex app ChatGPT relay"
APP_RELAY_NOTICE = "Restart the Codex app and sign in with ChatGPT to use the relay"
# Codex's OS-managed settings; a relayed launch must neither write nor depend on it.
CODEX_MANAGED_CONFIG_PATH = "/etc/codex/managed_config.toml"


def _assert_managed_file_has_no_databricks_provider():
    if os.name != "posix":
        return
    try:
        with open(CODEX_MANAGED_CONFIG_PATH, "rb") as managed_file:
            managed = tomllib.load(managed_file)
    except FileNotFoundError:
        return
    providers = managed.get("model_providers") or {}
    assert "Databricks" not in providers and managed.get("model_provider") != "Databricks", (
        f"A relayed launch left a Databricks provider in {CODEX_MANAGED_CONFIG_PATH}: {managed}"
    )


def test_ug_codex_relayed_completes_inference_through_the_relay(
    live_session, workspace, codex_relayed_provider, codex_access_token
):
    """Scenario: from fresh state, launch Codex relayed through a ChatGPT-relay MPS and submit a
    headless file task on a model the ChatGPT plan serves.

    Expected: the relayed launch runs headless (the access token stands in for the browser
    login). Codex completes the file task and its structured answer carries the file's value; the
    request went through ug's loopback relay proxy (route=forward on /v1/responses); the relayed
    session records `codex_relayed`; and the OS-managed file holds no Databricks provider.
    """
    session = live_session
    session.choose_codex_windows_sandbox()
    # Headless relayed launch: the access token stands in for the browser ChatGPT login, and the
    # diagnostics flag makes the proxy log its per-request route decision to stderr.
    session.env["CODEX_ACCESS_TOKEN"] = codex_access_token
    session.env["UCODE_RELAYED_PROXY_DIAGNOSTICS"] = "1"
    session.record("model.json", {"model": CODEX_RELAYED_MODEL, "source": "ChatGPT plan model"})
    task = FileTask(session)

    # This first launch also sets up the workspace, through the relayed provider.
    result = session.run(
        "codex",
        "--workspace",
        workspace,
        "--provider",
        codex_relayed_provider,
        "--",
        "exec",
        "--skip-git-repo-check",
        "--json",
        "-m",
        CODEX_RELAYED_MODEL,
        task.prompt,
        timeout=240,
    )

    task.assert_headless_answer("codex", result)
    assert '"route":"forward"' in result.stderr and '"path":"/v1/responses"' in result.stderr, (
        f"Relayed launch did not send its inference through the loopback relay:\n{result.stderr}"
    )
    assert session.workspace_state().get("codex_relayed") is True
    session.assert_not_routed()
    _assert_managed_file_has_no_databricks_provider()


def test_ug_codex_configure_relayed_app_entry_and_revert(
    live_session, workspace, codex_relayed_provider
):
    """Scenario: choose the relayed MPS in ug configure's provider picker for Codex, beside
    user-owned ~/.codex/config.toml settings, then revert.

    Expected: ug saves the provider and marks the setup relayed, and the OS-managed file holds no
    Databricks provider. The Codex app's `DatabricksRelay` entry (with `requires_openai_auth`) is
    written exactly when its login service could be set up: ug warns instead and leaves the file
    untouched when the host has no service manager (a CI Linux runner has no systemd user bus), so
    the entry is asserted only for the outcome ug reports. Revert removes the entry and keeps the
    user's settings. The service unit, the Codex app, and inference through the app are not covered.
    """
    session = live_session
    shared_config = session.home / ".codex" / "config.toml"
    # Ordinary pre-existing user settings, not manufactured gateway state.
    user_settings = "# user-owned Codex settings\n[notice]\nhide_rate_limit_model_nudge = true\n"
    shared_config.parent.mkdir(parents=True)
    shared_config.write_text(user_settings)

    command = [
        str(session.binary),
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "codex", command, "configure-relayed") as configure:
        configure.select_agent("Codex")
        configure.choose("How should Codex get its models?", "External Models")
        configure.choose("Select a model provider service:", codex_relayed_provider)
        configure.finish(timeout=240)
        transcript = " ".join(session.redact("".join(configure.output)).split())

    # ug reports exactly one outcome for the app entry.
    service_failed = APP_RELAY_WARNING in transcript
    assert service_failed != (APP_RELAY_NOTICE in transcript), transcript

    state = session.workspace_state()
    assert state["provider_services"]["codex"] == codex_relayed_provider
    assert state.get("codex_relayed") is True
    _assert_managed_file_has_no_databricks_provider()
    configured = tomllib.loads(shared_config.read_text())
    if service_failed:
        assert configured == tomllib.loads(user_settings), configured
    else:
        entry = configured["model_providers"][APP_RELAY_PROVIDER]
        assert configured["model_provider"] == APP_RELAY_PROVIDER
        assert entry["requires_openai_auth"] is True
        assert "auth" not in entry, entry
        assert entry["base_url"].startswith("http://127.0.0.1:"), entry
        assert entry["http_headers"]["Databricks-Model-Provider-Service"] == codex_relayed_provider
        assert {key: configured[key] for key in tomllib.loads(user_settings)} == tomllib.loads(
            user_settings
        )

    with TerminalProcess(session, "ug", [str(session.binary), "revert"], "relayed-revert") as tty:
        tty.finish()
    reverted = tomllib.loads(shared_config.read_text())
    assert APP_RELAY_PROVIDER not in (reverted.get("model_providers") or {}), reverted
    assert reverted.get("model_provider") != APP_RELAY_PROVIDER, reverted
    assert {key: reverted[key] for key in tomllib.loads(user_settings)} == tomllib.loads(
        user_settings
    )
