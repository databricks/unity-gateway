"""CUJs for Copilot Chat in VS Code: ug's setup, and chat turns driven through the real window."""

import json

import pytest
from utils.agents.vscode import chat_requests
from utils.constants import CLAUDE_TEST_MODEL, COPILOT_GPT_TEST_MODEL
from utils.evidence import FileTask, assert_no_bearer_on_disk
from utils.managed import use_managed_config_fixture
from utils.request_recorder import TuiRequestRecorder, assert_served
from utils.vscode import (
    VSCode,
    copilot_chat_version,
    installed_extensions,
    model_label,
    ug_extension_config,
    vscode_user_dir,
)

pytestmark = [pytest.mark.live, pytest.mark.vscode]

EXTENSION_ID = "databricks.unity-gateway"
CLAUDE_ROUTE = "/ai-gateway/anthropic/v1/messages"
GPT_ROUTE = "/ai-gateway/mlflow/v1/responses"
PROVIDER_GPT_ROUTE = "/ai-gateway/openai/v1/responses"
PROVIDER_HEADER = "databricks-model-provider-service"
# The http_headers entry in fixtures/managed_config/copilot_vscode_defaults.json.
MANAGED_HEADER = ("x-ug-integration-lane", "vscode")


def _configure(session, workspace, *options):
    return session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        *options,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=300,
    )


def _user_settings(session):
    # Settings a developer already had are user-owned input that ug must leave as they were.
    path = vscode_user_dir(session) / "User/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"editor.fontSize": 15, "files.autoSave": "afterDelay"}, indent=2))
    return path


def _assert_turn(session, recorder, checkpoint, task, answer, model, route, headers=None):
    """The chat answer carries the file's value, and every request of the turn asked the gateway
    for ``model`` on ``route`` with ``headers``, from the bundled Copilot Chat, and got a non-empty
    HTTP 200."""
    assert task.value in answer, answer
    requests = [
        request
        for request in recorder.requests_after(checkpoint)
        if request.method == "POST" and request.path.startswith("/ai-gateway/")
    ]
    session.record(
        f"gateway-requests-{model}.json",
        [
            {"path": r.path, "model": r.payload.get("model"), "ua": r.headers.get("user-agent")}
            for r in requests
        ],
    )
    assert requests, "The chat turn reached no gateway route"
    agent = f"githubcopilotchat/{copilot_chat_version(session)}"
    for request in requests:
        assert request.path == route, request.path
        user_agent = request.headers.get("user-agent", "")
        assert user_agent.startswith("ucode/") and user_agent.endswith(agent), user_agent
        for name, value in (headers or {}).items():
            assert request.headers.get(name) == value, (name, request.headers.get(name))
        assert_served(recorder, request, model)


def test_ug_configure_copilot_vscode_installs_and_reverts_the_extension(live_session, workspace):
    """Scenario: a developer with VS Code and their own settings runs `ug configure --agents
    copilot` twice, then `ug revert`.

    Expected: VS Code's own `code --list-extensions` lists ug's extension after each configure, and
    exactly one copy is installed. Its ug-config.json names the workspace, a sign-in command, and
    the pinned Claude and GPT test models on the Anthropic Messages and Responses wires. No bearer is
    saved on disk. After revert, VS Code no longer lists the extension and the developer's settings
    file is byte-identical. Configuration only: the chat CUJs below cover inference.
    """
    session = live_session
    settings = _user_settings(session)
    before = settings.read_bytes()

    _configure(session, workspace)
    assert EXTENSION_ID in installed_extensions(session)
    config = ug_extension_config(session)
    session.record("ug-config.json", config)
    assert config["workspace"] == workspace
    assert config["authCommand"]
    wires = {entry["id"]: entry["api"] for entry in config["models"]}
    assert wires.get(CLAUDE_TEST_MODEL) == "anthropic", wires
    assert wires.get(COPILOT_GPT_TEST_MODEL) == "responses", wires

    _configure(session, workspace)
    assert EXTENSION_ID in installed_extensions(session)
    assert ug_extension_config(session) == config
    assert_no_bearer_on_disk(session)

    session.run("revert", timeout=300)
    assert EXTENSION_ID not in installed_extensions(session)
    assert settings.read_bytes() == before


@pytest.mark.harness_latest
def test_vscode_copilot_chat_switches_from_claude_to_gpt_in_one_window(live_session, workspace):
    """Scenario: after `ug configure --agents copilot`, a developer opens VS Code for the first time,
    continues without signing in to GitHub, trusts the folder, adds the model providers from Copilot
    Chat's model picker, picks the Databricks Claude test model and asks Agent mode to read a local
    file. In the same window they pick the GPT test model and ask it to read another file.

    Expected: everything happens through visible clicks and keys on a fresh profile; no settings or
    VS Code state is seeded. Each answer contains its own file's value, which is never in a prompt.
    Every request of the Claude turn reaches the gateway's Anthropic Messages route, and every
    request of the GPT turn the Responses route, through a recording forwarder, for the picked
    model, with ug's User-Agent naming the Copilot Chat bundled in the pinned VS Code, and gets a
    non-empty HTTP 200, so no turn falls back to another model. VS Code's saved chat records both
    turns on their Databricks models, with tool calls, the values, and no error. After the window
    closes, no file under the home holds the bearer. Limits: one pinned VS Code, signed out of
    GitHub, Copilot Chat's local agent.
    """
    session = live_session
    with TuiRequestRecorder(workspace) as recorder:
        _configure(session, recorder.url)
        config = ug_extension_config(session)
        with VSCode(session, "switch") as vscode:
            vscode.finish_first_launch()
            vscode.pick_model(model_label(config, CLAUDE_TEST_MODEL))
            claude_task = FileTask(session)
            checkpoint = recorder.checkpoint()
            answer = vscode.wait_for_answer(vscode.send(claude_task.prompt))
            _assert_turn(
                session, recorder, checkpoint, claude_task, answer, CLAUDE_TEST_MODEL, CLAUDE_ROUTE
            )

            vscode.pick_model(model_label(config, COPILOT_GPT_TEST_MODEL))
            gpt_task = FileTask(session)
            checkpoint = recorder.checkpoint()
            answer = vscode.wait_for_answer(vscode.send(gpt_task.prompt))
            _assert_turn(
                session, recorder, checkpoint, gpt_task, answer, COPILOT_GPT_TEST_MODEL, GPT_ROUTE
            )
            turns = chat_requests(session, 2)
    for turn, model, task in (
        (turns[0], CLAUDE_TEST_MODEL, claude_task),
        (turns[1], COPILOT_GPT_TEST_MODEL, gpt_task),
    ):
        assert turn["model"] == f"databricks-unity-gateway/{model}", turn
        assert turn["tools"], turn
        assert turn["error"] is None, turn
        assert task.value in turn["text"], turn
    assert_no_bearer_on_disk(session)


def test_vscode_copilot_chat_reads_a_file_on_a_provider_service_model(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: a developer runs `ug configure --agents copilot --provider` with an OpenAI Model
    Provider Service, opens VS Code for the first time as in the switch CUJ, picks the service's
    GPT model in Copilot Chat's model picker and asks Agent mode to read a local file.

    Expected: the extension lists the service's own model id. The answer contains the file's value,
    which is never in the prompt. Every request of the turn reaches the gateway's OpenAI Responses
    route with the provider service header, for the service's model, with ug's User-Agent, and gets
    a non-empty HTTP 200. This shows the request named the service; which backend served it is the
    gateway's side and is not asserted here.
    """
    session = live_session
    with TuiRequestRecorder(workspace) as recorder:
        _configure(session, recorder.url, "--provider", codex_provider)
        config = ug_extension_config(session)
        session.record("ug-config.json", config)
        assert config["provider"] == codex_provider
        label = model_label(config, codex_provider_model)
        with VSCode(session, "provider") as vscode:
            vscode.finish_first_launch()
            vscode.pick_model(label)
            task = FileTask(session)
            checkpoint = recorder.checkpoint()
            answer = vscode.wait_for_answer(vscode.send(task.prompt))
            _assert_turn(
                session,
                recorder,
                checkpoint,
                task,
                answer,
                codex_provider_model,
                PROVIDER_GPT_ROUTE,
                {PROVIDER_HEADER: codex_provider},
            )


@pytest.mark.managed_fixture
def test_vscode_copilot_chat_starts_on_the_managed_default_model(live_session, workspace):
    """Scenario: under the injected `copilot_vscode_defaults` managed config (the Claude and GPT
    test models, GPT as the default, an admin HTTP header), a developer runs `ug configure`, opens
    VS Code for the first time as in the switch CUJ, and asks Agent mode to read a local file
    without touching the model picker's model list.

    Expected: once the model providers are added, the chat's model is the managed default, not
    the first model VS Code would pick by itself. The answer contains the file's value. Every
    request of the turn asks for the default model on the Responses route, carries the admin's
    header and ug's User-Agent, and gets a non-empty HTTP 200.
    """
    session = live_session
    use_managed_config_fixture(session, "copilot_vscode_defaults")
    with TuiRequestRecorder(workspace) as recorder:
        session.run(
            "configure",
            "--workspace",
            recorder.url,
            "--skip-validate",
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=300,
        )
        config = ug_extension_config(session)
        session.record("ug-config.json", config)
        assert config["defaultModel"] == COPILOT_GPT_TEST_MODEL
        assert {entry["id"] for entry in config["models"]} == {
            CLAUDE_TEST_MODEL,
            COPILOT_GPT_TEST_MODEL,
        }
        with VSCode(session, "managed") as vscode:
            vscode.finish_first_launch()
            vscode.pick_model(model_label(config, COPILOT_GPT_TEST_MODEL), select=False)
            task = FileTask(session)
            checkpoint = recorder.checkpoint()
            answer = vscode.wait_for_answer(vscode.send(task.prompt))
            _assert_turn(
                session,
                recorder,
                checkpoint,
                task,
                answer,
                COPILOT_GPT_TEST_MODEL,
                GPT_ROUTE,
                dict([MANAGED_HEADER]),
            )


def test_vscode_copilot_chat_lists_the_new_models_after_reconfigure(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: a developer uses Copilot Chat on the Databricks Claude test model, quits VS Code,
    runs `ug configure --agents copilot --provider` with an OpenAI Model Provider Service, and
    reopens VS Code on the same profile.

    Expected: the first turn completes as in the switch CUJ. After the reconfigure, the extension
    lists only the service's models; in the reopened window, searching the model picker finds no
    workspace Claude model, and picking the service's model completes a turn whose requests reach
    the OpenAI Responses route with the provider service header, for the service's model, with a
    non-empty HTTP 200. Not covered: what the chat shows for a model that went away, which the
    developer has to replace by picking a new one.
    """
    session = live_session
    with TuiRequestRecorder(workspace) as recorder:
        _configure(session, recorder.url)
        old_label = model_label(ug_extension_config(session), CLAUDE_TEST_MODEL)
        with VSCode(session, "first") as vscode:
            vscode.finish_first_launch()
            vscode.pick_model(old_label)
            task = FileTask(session)
            checkpoint = recorder.checkpoint()
            answer = vscode.wait_for_answer(vscode.send(task.prompt))
            _assert_turn(
                session, recorder, checkpoint, task, answer, CLAUDE_TEST_MODEL, CLAUDE_ROUTE
            )
            vscode.quit()

        _configure(session, recorder.url, "--provider", codex_provider)
        config = ug_extension_config(session)
        session.record("ug-config.json", config)
        ids = {entry["id"] for entry in config["models"]}
        assert codex_provider_model in ids and CLAUDE_TEST_MODEL not in ids, ids
        with VSCode(session, "reopened") as vscode:
            vscode.assert_not_in_picker(old_label)
            vscode.pick_model(model_label(config, codex_provider_model))
            task = FileTask(session)
            checkpoint = recorder.checkpoint()
            answer = vscode.wait_for_answer(vscode.send(task.prompt))
            _assert_turn(
                session,
                recorder,
                checkpoint,
                task,
                answer,
                codex_provider_model,
                PROVIDER_GPT_ROUTE,
                {PROVIDER_HEADER: codex_provider},
            )
