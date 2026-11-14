"""CUJ: the gateway can tell Copilot CLI sessions and subagents apart."""

import uuid

import pytest
from utils.constants import CLAUDE_TEST_MODEL, COPILOT_HEADLESS_FLAGS
from utils.evidence import fresh_id
from utils.request_recorder import TuiRequestRecorder

pytestmark = [pytest.mark.live, pytest.mark.copilot]

INFERENCE_PATH = "/ai-gateway/anthropic/v1/messages"


def test_ug_copilot_sends_one_session_id_and_an_id_per_subagent(live_session, workspace):
    """Scenario: configure Copilot through a recording proxy in front of the workspace, then
    run a headless prompt that delegates two lookups to subagents.

    Expected: every model request carries the same `session-id`, a UUID; each subagent's
    requests carry a `thread-id` of that session, the same over the subagent's turns and
    different between the two subagents; the main agent's requests carry none.
    """
    session = live_session
    markers = {}
    for name in ("north", "south"):
        marker = fresh_id()
        folder = session.cwd / name
        folder.mkdir()
        (folder / f"{marker}.txt").write_text("x\n", encoding="utf-8")
        markers[name] = marker

    with TuiRequestRecorder(workspace) as recorder:
        session.run(
            "configure",
            "--agents",
            "copilot",
            "--workspace",
            recorder.url,
            "--skip-validate",
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )
        checkpoint = recorder.checkpoint()
        result = session.run(
            "copilot",
            "--model",
            CLAUDE_TEST_MODEL,
            "-p",
            "Call the task tool twice in parallel, with agent_type explore: one subagent "
            "reports the name of the only file in the folder north, the other the name of the "
            "only file in the folder south. Don't look at the folders yourself. Then reply "
            "with both file names.",
            *COPILOT_HEADLESS_FLAGS,
            timeout=300,
        )
        requests = [
            request
            for request in recorder.requests_after(checkpoint)
            if request.path == INFERENCE_PATH
        ]

    assert markers["north"] in result.stdout and markers["south"] in result.stdout, result.stdout
    session_ids = {request.headers.get("session-id") for request in requests}
    assert len(session_ids) == 1, session_ids
    (session_id,) = session_ids
    assert str(uuid.UUID(session_id)) == session_id
    subagents = [
        request
        for request in requests
        if request.headers.get("x-interaction-type") == "conversation-subagent"
    ]
    main = [request for request in requests if request not in subagents]
    assert main and all("thread-id" not in request.headers for request in main)
    threads = {request.headers.get("thread-id") for request in subagents}
    assert len(threads) == 2, threads
    assert all(thread and thread.startswith(f"{session_id}-") for thread in threads)
    assert len(subagents) > len(threads), "each subagent should take more than one turn"
