"""CUJs for using Grok Build from scripts through installed ug."""

import hashlib
import json
import tomllib

import pytest
from utils.agents import grok
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL, GROK_HEADLESS_FLAGS
from utils.evidence import FileTask, assert_no_bearer_on_disk, fresh_id

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


def _ask(session, prompt, *allow_rules):
    """A headless prompt that may use only the tools ``allow_rules`` (besides reads) permit."""
    allowed = [arg for rule in allow_rules for arg in ("--allow", rule)]
    result = session.run("grok", "--", *GROK_HEADLESS_FLAGS, *allowed, "-p", prompt, timeout=300)
    records = grok.events(result.stdout)
    assert grok.end_event(records).get("stopReason") == "end_turn", result.stdout
    return records


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


def test_ug_grok_headless_anthropic_mps(
    live_session, workspace, claude_provider, claude_provider_model
):
    """Scenario: after a plain Databricks Hosted configure, run a headless task through the real
    API-key Anthropic Model Provider Service with `ug grok --provider` and no `--model`.

    Expected: the launch registers the service's Claude targets in place of the workspace's
    models and starts on one of them; Grok reads the fixture and answers on that target through
    the session proxy, and no file under the session home holds the Databricks credential. This
    proves Grok's request, not the gateway's destination.
    """
    session = live_session
    task = FileTask(session)
    _configure(session, workspace)
    session.record("grok-provider.json", {"provider": claude_provider})

    records = _run_file_task(session, task, "--provider", claude_provider)

    config = _grok_config(session)
    assert claude_provider_model in config["model"], config["model"]
    assert not any(name.startswith("system.ai.") for name in config["model"]), config["model"]
    assert grok.turn_models(records) == {config["models"]["default"]}
    _assert_models_reach_only_the_session_proxy(session)
    assert_no_bearer_on_disk(session)


@pytest.mark.parametrize(
    ("provider_fixture", "model_fixture"),
    [
        ("codex_provider", "codex_provider_model"),
        ("codex_foundry_provider", "codex_foundry_provider_model"),
    ],
    ids=["openai", "microsoft-foundry"],
)
def test_ug_grok_headless_openai_api_mps(
    live_session, workspace, request, provider_fixture, model_fixture
):
    """Scenario: run a headless task through a real OpenAI-API Model Provider Service (OpenAI,
    Microsoft Foundry) with `ug grok --provider ... --model <target>`.

    Expected: Grok reaches the service's model on the gateway's native OpenAI route through the
    session proxy, reads the fixture, and the turn is served by the named target.
    """
    session = live_session
    provider = request.getfixturevalue(provider_fixture)
    model = request.getfixturevalue(model_fixture)
    task = FileTask(session)
    _configure(session, workspace)
    session.record("grok-provider.json", {"provider": provider, "model": model})

    records = _run_file_task(
        session, task, "--provider", provider, "--model", model, prompt=task.prompt_with_path
    )

    assert grok.turn_models(records) == {model}
    entry = _grok_config(session)["model"][model]
    assert entry["base_url"] == "${UG_GROK_PROXY_URL}/ai-gateway/openai/v1", entry


def test_ug_grok_mcp_sql_tool(live_session, workspace):
    """Scenario: register the Databricks SQL MCP service for Grok with `ug mcp add`, then ask a
    headless session, allowed only that server's tools, to run a query through it.

    Expected: ug writes a stdio `ug mcp-proxy` entry to Grok's private config, Grok completes an
    MCP tool call on that server, and its answer is the query's md5 result, which the model
    cannot compute itself.
    """
    session = live_session
    _configure(session, workspace)
    session.run("mcp", "add", "--names", "system.ai.dbsql", "--agents", "grok", timeout=240)
    entry = _grok_config(session)["mcp_servers"]["system-ai-dbsql"]
    assert entry["args"][0] == "mcp-proxy", entry
    assert any("system.ai.dbsql" in arg for arg in entry["args"]), entry

    value = fresh_id()
    digest = hashlib.md5(value.encode()).hexdigest()  # noqa: S324 (query output, not security)
    records = _ask(
        session,
        "Use the system-ai-dbsql MCP server's SQL tool to run exactly: "
        f"SELECT md5('{value}') AS h. Reply with only the value it returned.",
        "MCPTool(system-ai-dbsql__*)",
    )

    assert grok.completed_mcp_calls(records, "system-ai-dbsql"), records
    assert digest in grok.answer(records), records


def test_ug_grok_loads_downloaded_skill_directory(live_session, workspace):
    """Scenario: a user has a skill in `~/.agents/skills` (where `ug skills add` downloads), and
    asks a headless Grok session launched through ug for it by name.

    Expected: Grok, running on ug's private home, still discovers the skill, reads its SKILL.md,
    and answers with the code word only that file holds.
    """
    session = live_session
    name = "ug-check-" + fresh_id().lower()
    code_word = fresh_id()
    skill = session.home / ".agents/skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: Gives the {name} code word. Use when asked for it.\n"
        f"---\nThe {name} code word is {code_word}.\n"
    )
    _configure(session, workspace)

    records = _ask(session, f"Use the {name} skill and reply with only the code word it gives.")

    assert any(name in json.dumps(read) for read in grok.completed_reads(records)), records
    assert code_word in grok.answer(records), records
