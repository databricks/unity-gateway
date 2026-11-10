"""CUJs for Copilot Chat in VS Code: ug's setup, and chat turns driven through the real window."""

import json

import pytest
from utils.agents.vscode import chat_requests
from utils.constants import CLAUDE_TEST_MODEL, COPILOT_GPT_TEST_MODEL
from utils.evidence import FileTask, assert_no_bearer_on_disk
from utils.request_recorder import TuiRequestRecorder, assert_served
from utils.vscode import (
    VSCode,
    copilot_chat_version,
    installed_extensions,
    model_label,
    ug_extension_config,
)

pytestmark = [pytest.mark.live, pytest.mark.vscode]

EXTENSION_ID = "databricks.unity-gateway"
CLAUDE_ROUTE = "/ai-gateway/anthropic/v1/messages"
GPT_ROUTE = "/ai-gateway/mlflow/v1/responses"


def _configure(session, workspace):
    return session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=300,
    )


def _user_settings(session):
    # Settings a developer already had are user-owned input that ug must leave as they were.
    path = session.home / ".config/Code/User/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"editor.fontSize": 15, "files.autoSave": "afterDelay"}, indent=2))
    return path


def _assert_turn(session, recorder, checkpoint, task, answer, model, route):
    """The chat answer carries the file's value, and every request of the turn asked the gateway
    for ``model`` on ``route``, from the bundled Copilot Chat, and got a non-empty HTTP 200."""
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


def test_vscode_copilot_chat_reads_a_file_on_a_databricks_claude_model(live_session, workspace):
    """Scenario: after `ug configure --agents copilot`, a developer opens VS Code for the first time,
    continues without signing in to GitHub, trusts the folder, adds the model providers from Copilot
    Chat's model picker, picks the Databricks Claude test model and asks Agent mode to read a local
    file.

    Expected: everything happens through visible clicks and keys on a fresh profile; no settings or
    VS Code state is seeded. The chat answer contains the file's value, which is never in the
    prompt. Every request of the turn reaches the gateway's Anthropic Messages route through a
    recording forwarder, for the picked model, with ug's User-Agent naming the Copilot Chat bundled
    in the pinned VS Code, and gets a non-empty HTTP 200. VS Code's saved chat records the
    Databricks model, a tool call, the value, and no error. Limits: Linux only, one pinned VS Code,
    signed out of GitHub, Copilot Chat's local agent.
    """
    session = live_session
    with TuiRequestRecorder(workspace) as recorder:
        _configure(session, recorder.url)
        label = model_label(ug_extension_config(session), CLAUDE_TEST_MODEL)
        with VSCode(session, "claude") as vscode:
            vscode.finish_first_launch()
            vscode.pick_model(label)
            task = FileTask(session)
            checkpoint = recorder.checkpoint()
            answer = vscode.wait_for_answer(vscode.send(task.prompt))
            _assert_turn(
                session, recorder, checkpoint, task, answer, CLAUDE_TEST_MODEL, CLAUDE_ROUTE
            )
            [turn] = chat_requests(session, 1)
    assert turn["model"] == f"databricks-unity-gateway/{CLAUDE_TEST_MODEL}", turn
    assert turn["tools"], turn
    assert turn["error"] is None, turn
    assert task.value in turn["text"], turn


def test_vscode_copilot_chat_switches_from_claude_to_gpt_in_one_window(live_session, workspace):
    """Scenario: in one VS Code window set up as above, a developer asks the Databricks Claude test
    model to read one local file, then picks the GPT test model in the model picker and asks it to
    read another.

    Expected: each answer contains its own file's value, which is never in a prompt. The first turn's
    requests reach the Anthropic Messages route for Claude and the second turn's the Responses route
    for GPT, each with ug's User-Agent and a non-empty HTTP 200, so no turn falls back to another
    model. VS Code's saved chat records both turns on their Databricks models, with tool calls and no
    error. Limits as in the Claude CUJ.
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
