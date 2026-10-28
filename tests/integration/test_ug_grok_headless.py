"""CUJs for using Grok Build from scripts through installed ug."""

import tomllib

import pytest
from utils.agents import grok
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL, GROK_HEADLESS_FLAGS
from utils.evidence import FileTask, assert_no_bearer_on_disk

pytestmark = [pytest.mark.live, pytest.mark.grok]

GROK_CONFIG = ".ucode/grok-home/config.toml"


def _configure(session, workspace):
    session.run(
        "configure",
        "--agents",
        "grok",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )


def _grok_config(session) -> dict:
    return tomllib.loads((session.home / GROK_CONFIG).read_text(encoding="utf-8"))


def _assert_models_reach_only_the_session_proxy(session) -> None:
    """ug's models name per-session placeholders, never a gateway URL or credential."""
    models = _grok_config(session)["model"]
    assert models, "ug wrote no Grok models"
    for name, entry in models.items():
        assert entry["base_url"].startswith("${UG_GROK_PROXY_URL}/ai-gateway/"), name
        assert entry["env_key"] == "UG_GROK_SESSION_SECRET", name
        assert "api_key" not in entry, name


def _run_file_task(session, task, *model_args, prompt=None):
    result = session.run(
        "grok",
        *model_args,
        "--",
        *GROK_HEADLESS_FLAGS,
        "-p",
        prompt or task.prompt,
        timeout=240,
    )
    task.assert_headless_answer("grok", result)
    return grok.events(result.stdout)


def test_ug_grok_headless_completes_file_task(live_session, workspace):
    """Scenario: configure Grok Build, then run a headless Claude prompt through `ug grok`.

    Expected: Grok reads the fixture with its file tool through the session's loopback proxy on
    the Anthropic Messages route, and its final answer contains the unknown value; the turn was
    served by the requested model; ug's models name only the session proxy and secret, and no
    file under the session home holds the Databricks credential.
    """
    session = live_session
    task = FileTask(session)
    _configure(session, workspace)
    session.record("grok-model.json", {"model": CLAUDE_TEST_MODEL})

    records = _run_file_task(session, task, "--model", CLAUDE_TEST_MODEL)

    assert CLAUDE_TEST_MODEL in grok.turn_models(records)
    _assert_models_reach_only_the_session_proxy(session)
    assert_no_bearer_on_disk(session)


def test_ug_grok_headless_gpt_responses_task(live_session, workspace):
    """Scenario: configure Grok Build, then run a headless GPT prompt through `ug grok`.

    Expected: Grok reaches the gateway's Responses API through the session proxy, reads the
    fixture with its file tool, and its final answer (served by the requested model) contains
    the unknown value; no file under the session home holds the Databricks credential.
    """
    session = live_session
    task = FileTask(session)
    _configure(session, workspace)
    session.record("grok-model.json", {"model": CODEX_TEST_MODEL})

    records = _run_file_task(
        session, task, "--model", CODEX_TEST_MODEL, prompt=task.prompt_with_path
    )

    assert CODEX_TEST_MODEL in grok.turn_models(records)
    assert_no_bearer_on_disk(session)


def test_ug_grok_headless_default_model_task(live_session, workspace):
    """Scenario: configure Grok Build, then run a headless prompt with no model chosen.

    Expected: the session starts on the default `ug configure` wrote for the workspace (its
    newest Grok model when it serves one), the turn is served by that model, and the final
    answer contains the fixture's unknown value; no file under the session home holds the
    Databricks credential.
    """
    session = live_session
    task = FileTask(session)
    _configure(session, workspace)
    default = _grok_config(session)["models"]["default"]
    session.record("grok-model.json", {"model": default})

    records = _run_file_task(session, task)

    assert default in grok.turn_models(records)
    assert_no_bearer_on_disk(session)


def test_ug_grok_unknown_model_lists_available_models(live_session, workspace):
    """Scenario: configure Grok Build, then launch with a model the workspace doesn't have.

    Expected: exit 1 before Grok starts (it records no session); stderr names the model and
    lists the available ones.
    """
    session = live_session
    _configure(session, workspace)

    result = session.run(
        "grok",
        "--model",
        "no-such-model",
        "--",
        *GROK_HEADLESS_FLAGS,
        "-p",
        "hi",
        timeout=120,
        ok=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    stderr = " ".join(result.stderr.split())
    assert "Model 'no-such-model' isn't available in ug grok" in stderr
    assert CLAUDE_TEST_MODEL in stderr
    assert not (session.home / ".ucode/grok-home/sessions").exists()
