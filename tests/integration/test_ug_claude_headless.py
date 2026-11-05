"""CUJs for using claude from scripts through installed ug."""

import json

import pytest
from utils.constants import CLAUDE_TEST_MODEL
from utils.evidence import FileTask, assert_completed_task_model

pytestmark = [pytest.mark.live, pytest.mark.claude]

# The filtered Bedrock runtime MPS declares this inference profile; Bedrock reports the
# bare Anthropic id as the response model.
BEDROCK_RUNTIME_OPUS_MODEL = "us.anthropic.claude-opus-5-5"
BEDROCK_OPUS_RESPONSE_MODEL = "claude-opus-5-5"


@pytest.mark.smoke
def test_ug_claude_headless_prompt_argument(live_session, workspace):
    """Scenario: configure claude and submit a headless prompt via argument.

    Expected: the real agent completes the file task with JSON-only stdout while
    opt-in UG timing records reach stderr through process handoff.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    session.env["UG_STARTUP_TIMING"] = "1"
    result = session.run(
        "claude",
        "--",
        "-p",
        task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        timeout=180,
    )
    assert json.loads(result.stdout)["type"] == "result"
    task.assert_headless_answer("claude", result)
    session.assert_not_routed()
    timings = [
        json.loads(line)
        for line in result.stderr.splitlines()
        if line.startswith('{"event":"ug_startup_timing"')
    ]
    assert {"entrypoint", "cli_import", "bootstrap", "harness_handoff"} <= {
        record["stage"] for record in timings
    }, result.stderr
    for record in timings:
        assert set(record) == {"event", "stage", "outcome", "pid", "elapsed_ms", "duration_ms"}
        assert record["elapsed_ms"] >= 0 and record["duration_ms"] >= 0


def test_ug_claude_headless_prompt_stdin(live_session, workspace):
    """Scenario: configure claude and submit a headless prompt via stdin.

    Expected: the real agent reads the fixture and returns its unknown value in
    its structured completed answer, with exit code zero and JSON-only stdout.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    result = session.run(
        "claude",
        "--",
        "-p",
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        timeout=180,
        input_text=task.prompt + "\n",
    )
    assert json.loads(result.stdout)["type"] == "result"
    task.assert_headless_answer("claude", result)
    session.assert_not_routed()


def test_ug_claude_headless_prompt_after_separator(live_session, workspace):
    """Scenario: configure claude and submit a headless prompt via after separator.

    Expected: the real agent reads the fixture and returns its unknown value in
    its structured completed answer, with exit code zero.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    result = session.run(
        "claude",
        "--",
        "-p",
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        "--",
        task.prompt,
        timeout=180,
    )
    task.assert_headless_answer("claude", result)
    session.assert_not_routed()


@pytest.mark.usefixtures("unmanaged_workspace")
def test_ug_claude_headless_fresh_workspace(live_session, workspace):
    """Scenario: launch Claude headlessly against an unmanaged workspace from fresh state.

    Expected: ``ug claude --workspace`` starts the real installed Claude CLI without a
    configure step, and its structured result contains the unpredictable fixture value after
    using the Read tool with the inexpensive Haiku model; the command exits without routing.
    """
    session = live_session
    task = FileTask(session)

    result = session.run(
        "claude",
        "--workspace",
        workspace,
        "--",
        "--model",
        CLAUDE_TEST_MODEL,
        "-p",
        task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        timeout=180,
    )
    task.assert_headless_answer("claude", result)
    usage = json.loads(result.stdout)["modelUsage"]
    assert set(usage) == {CLAUDE_TEST_MODEL}, usage
    assert usage[CLAUDE_TEST_MODEL]["outputTokens"] > 0, usage
    session.assert_not_routed()


@pytest.mark.usefixtures("unmanaged_workspace")
def test_ug_claude_fresh_provider_launch(live_session, workspace, claude_provider):
    """Scenario: launch Claude from fresh state with an explicit provider service.

    Expected: ``ug claude --workspace`` with ``--provider`` starts the real installed Claude
    CLI without a configure step, reports the selected provider, and writes the provider header
    to its generated settings; ``--version`` exits successfully without inference or routing.
    """
    session = live_session
    result = session.run(
        "claude",
        "--workspace",
        workspace,
        "--provider",
        claude_provider,
        "--",
        "--version",
        timeout=180,
    )
    assert claude_provider in result.stdout
    settings = json.loads((session.home / ".claude/ucode-settings.json").read_text())
    headers = (settings.get("env") or {}).get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
    assert f"Databricks-Model-Provider-Service: {claude_provider}" in headers
    session.assert_not_routed()


@pytest.mark.parametrize("model_form", ["separate", "equals"])
@pytest.mark.parametrize("model_owner", ["ug", "claude"])
def test_ug_claude_headless_explicit_model_bypasses_routing(
    live_session, workspace, model_form, model_owner
):
    """Scenario: choose a model before/after ug's separator with no workspace policy.

    Expected: with smart routing enabled, the real file task completes on the
    requested model, confirmed by JSON modelUsage, without a routing wrapper.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    model = session.model_for_explicit_case("claude")
    model_args = ["--model", model] if model_form == "separate" else [f"--model={model}"]
    session.env["ENABLE_SMART_ROUTING_V2"] = "1"
    result = session.run(
        "claude",
        *(model_args if model_owner == "ug" else []),
        "--",
        "-p",
        task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        *(model_args if model_owner == "claude" else []),
        timeout=180,
    )
    task.assert_headless_answer("claude", result)
    usage = json.loads(result.stdout)["modelUsage"]
    assert set(usage) == {model}, {"expected": model, "observed": sorted(usage)}
    assert usage[model]["outputTokens"] > 0, usage
    session.assert_not_routed()


def test_ug_claude_preserves_caller_settings_and_hook(live_session, workspace):
    """Scenario: a launcher passes a settings path containing spaces to ug claude.

    Expected: the caller's real SessionStart hook executes, its input file stays
    unchanged, and gateway authentication still supports a completed file task.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    settings = session.cwd / "caller settings.json"
    # Ordinary user-owned input, not fabricated ug state or generated gateway config.
    content = json.dumps(
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "echo caller-hook-ran > caller-hook.txt"}
                        ]
                    }
                ]
            }
        }
    )
    settings.write_text(content)
    result = session.run(
        "claude",
        "--",
        "-p",
        task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        "--settings",
        str(settings),
        timeout=180,
    )
    task.assert_headless_answer("claude", result)
    assert (session.cwd / "caller-hook.txt").read_text().strip() == "caller-hook-ran"
    assert settings.read_text() == content


def test_ug_claude_reports_unsupported_short_model_option(live_session, workspace):
    """Scenario: pass -m to the selected Claude version, which does not support it.

    Expected: ug preserves the actual agent's unknown-option error and status.
    This is an error-reporting journey, not a successful inference claim.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    expected = session.run("-m", "sonnet", "-p", "hi", binary=session.which("claude"), ok=False)
    actual = session.run("claude", "--", "-m", "sonnet", "-p", "hi", ok=False)
    assert expected.returncode != 0 and "unknown option '-m'" in expected.stderr
    assert actual.returncode == expected.returncode
    assert "unknown option '-m'" in actual.stderr


def test_ug_claude_headless_allow_all_bedrock_provider(
    live_session, workspace, claude_bedrock_allow_all_provider, claude_bedrock_allow_all_model
):
    """Scenario: launch Claude through a Bedrock MPS with allow_all_targets and no declared
    targets (issue #811), pinning the explicit Bedrock Sonnet 5.5 model.

    Expected: ug accepts the provider (rather than rejecting it as "exposes no Claude models"),
    and the real file task completes through Bedrock.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    result = session.run(
        "claude",
        "--provider",
        claude_bedrock_allow_all_provider,
        "--model",
        claude_bedrock_allow_all_model,
        "--",
        "-p",
        task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        timeout=240,
    )
    task.assert_headless_answer("claude", result)


def _run_bedrock_provider_headless(session, workspace, provider, model):
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    result = session.run(
        "claude",
        "--provider",
        provider,
        "--model",
        model,
        "--",
        "-p",
        task.prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        timeout=240,
    )
    task.assert_headless_answer("claude", result)
    return task


def test_ug_claude_headless_bedrock_runtime_opus(live_session, workspace, claude_bedrock_provider):
    """Scenario: launch Claude through the Bedrock runtime MPS (UC service-credential auth) that
    declares Sonnet 5.5 and Opus 5.5, pinning the Opus 5.5 inference profile.

    Expected: the real file task completes through Bedrock and Bedrock reports the bare Opus 5.5
    model id, so the pinned `us.anthropic.*` profile was the one served.
    """
    task = _run_bedrock_provider_headless(
        live_session, workspace, claude_bedrock_provider, BEDROCK_RUNTIME_OPUS_MODEL
    )
    assert_completed_task_model(live_session, "claude", task.value, BEDROCK_OPUS_RESPONSE_MODEL)
