"""CUJ 6: repeated configuration, mixed sources, and removal (AIGTWY-4881).

One developer home receives three successive admin configs. Each change must take effect
without leaving stale ug-managed values or touching the user's own settings.

Workspace configs are read only, so each phase is the single published config of its own
workspace and the shared home moves from A to B to C. All three share one metastore with the
``ug_e2e`` fixtures (the Bedrock Claude MPS ``ug_e2e.providers.bedrock``, the Codex model
services in ``ug_e2e.models``, and the ``fixture_reader`` MCP) and one trace table.

Managed skills are not covered: UC skill bundles cannot be uploaded on these workspaces'
Default Storage catalog (Files API HTTP 501).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from databricks.sdk import WorkspaceClient

from tests.integration.utils.evidence import FileTask, assert_no_terminal_api_error
from tests.integration.utils.sql import query_count, resolve_trace_table, resolve_warehouse_id
from tests.integration.utils.terminal import AgentTerminal, TerminalProcess
from ucode.agents.claude import CLAUDE_OTEL_TRACE_ENV_KEYS

from .base import BaseCujTest
from .helpers.constants import (
    BEDROCK_PROVIDER_SERVICE_FIXTURE,
    CLAUDE,
    CODEX,
    FIXTURE_READER_MCP_SERVICE_NAME,
    INFERENCE_PATHS,
    MANAGED_PATHS,
    UC_MODEL_LOCATION_FIXTURE,
    CodingAgent,
)
from .helpers.evidence import SessionEvidence, canonical_model
from .helpers.tui_request_recorder import TuiRequestRecorder
from .helpers.workspace import Workspace

CLAUDE_STATIC_MODELS = (
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
)
CLAUDE_DEFAULT = "system.ai.claude-sonnet-4-6"
CODEX_SOL = "system.ai.gpt-5-6-sol"
CODEX_LUNA = "system.ai.gpt-5-6-luna"
CLAUDE_PROVIDER, CLAUDE_PROVIDER_MODEL = BEDROCK_PROVIDER_SERVICE_FIXTURE
CODEX_MODEL_LOCATION, CODEX_UC_MODEL = UC_MODEL_LOCATION_FIXTURE
MANAGED_MCP_ENTRY = FIXTURE_READER_MCP_SERVICE_NAME.replace(".", "-")
CODEX_OS_MANAGED_CONFIG = MANAGED_PATHS[1]
RUN_HEADER = "x-ug-e2e-run"
AGENT_HEADER = "x-ug-e2e-agent"
PROVIDER_HEADER = "databricks-model-provider-service"
PARENT_SCHEMA_HEADER = "databricks-model-service-parent-schema"
CONFIGURE_ARGS = ["configure", "--skip-upgrade", "--disable-databricks-ai-tools"]
TRACE_DEADLINE_SECONDS = 300
TRACE_INGESTION_SECONDS = 300
USER_MCP = "ug-e2e-user-echo"
USER_SKILL = "ug-e2e-user-notes"
USER_MCP_SERVER = """\
import json, sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ug-e2e-user-echo", "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text()) if path.is_file() else {}


def model_leaf(model: str) -> str:
    """The model's family id, without catalog/vendor/region prefixes, [1m], date, or version."""
    name = model.rsplit(".", 1)[-1].removesuffix("[1m]")
    return re.sub(r"(-\d{8})?(-v\d+:\d+)?$", "", name)


def agent_config(config: dict, agent: CodingAgent) -> dict:
    return next(entry["config"] for entry in config["enabled_agents"] if entry["agent"] == agent)


def tracing_enabled(agent_settings: dict) -> bool:
    return bool(agent_settings.get("tracing", {}).get("enabled"))


def lower_keys(headers: dict) -> dict:
    return {key.lower(): value for key, value in headers.items()}


def mcp_tool_names(registration: dict) -> tuple[str, ...]:
    """Start a registered stdio MCP exactly as the agent would and list its tools."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "cuj6", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    result = subprocess.run(
        [registration["command"], *registration.get("args", [])],
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    responses = {row["id"]: row for row in map(json.loads, result.stdout.splitlines())}
    return tuple(tool["name"] for tool in responses[2]["result"]["tools"])


def workspace_client(url: str) -> WorkspaceClient:
    """The base class authenticates only WORKSPACE_URL; phases B and C need their own clients."""
    return WorkspaceClient(
        host=url,
        client_id=os.environ["UG_CUJ_SP_CLIENT_ID"],
        client_secret=os.environ["UG_CUJ_SP_CLIENT_SECRET"],
        auth_type="oauth-m2m",
    )


@dataclass(frozen=True)
class AgentFiles:
    """ug-written agent settings and MCP registrations in the shared home."""

    claude_settings: dict
    claude_user_settings: str
    codex_config: dict
    codex_ucode_config: dict
    codex_model_catalog: dict
    claude_mcp: dict
    codex_mcp: dict
    claude_mcp_list: tuple[str, ...]
    codex_mcp_list: tuple[str, ...]
    skill_files: dict[str, str]
    user_mcp_tools: dict[str, tuple[str, ...]]

    @property
    def claude_env(self) -> dict:
        return self.claude_settings.get("env", {})


@dataclass(frozen=True)
class AgentTask:
    """One headless task: its gateway inference requests and its native completed turn."""

    requests: tuple
    turn_models: tuple[str, ...]

    @property
    def request_models(self) -> set[str]:
        return {request.payload.get("model") for request in self.requests}


@dataclass(frozen=True)
class PhaseA:
    first: AgentFiles
    second: AgentFiles
    claude: AgentTask
    codex: AgentTask


@dataclass(frozen=True)
class PhaseB:
    files: AgentFiles
    restarted: AgentFiles
    claude: AgentTask
    codex: AgentTask
    bare_ug: AgentTask
    os_managed_codex: dict
    codex_span_count: int
    claude_span_count: int


@dataclass(frozen=True)
class PhaseC:
    files: AgentFiles
    final: AgentFiles
    os_managed_codex: dict
    codex: AgentTask
    picker: tuple[str, ...]
    disabled_claude_returncode: int
    disabled_claude_output: str
    disabled_claude_requests: int
    bare_ug: AgentTask
    phase_b_span_count: int
    codex_span_count: int


@dataclass(frozen=True)
class SeededUserState:
    claude_settings: str
    codex_keys: dict
    mcp_registrations: dict[str, dict]
    skill_files: dict[str, str]


class Journey:
    """The shared home moving through phase workspaces A, B, then C."""

    def __init__(self, session, workspaces, published, recorders):
        self.session = session
        self.workspaces = workspaces
        self.published = published
        self.recorders = recorders
        self.run_id = f"cuj6-{uuid.uuid4().hex[:12]}"
        bearer = self.bearer("a")
        # All phase workspaces share one metastore and trace table, so A queries them all.
        self.trace_url = workspaces["a"].url
        self.warehouse_id = resolve_warehouse_id(self.trace_url, bearer)
        self.trace_table = resolve_trace_table(self.trace_url, bearer)
        self.seeded = self.seed_user_state()

    @property
    def home(self) -> Path:
        return self.session.home

    def bearer(self, phase: str) -> str:
        # Each workspace issues its own M2M token; refreshed per call because the journey is long.
        headers = self.workspaces[phase].client.config.authenticate()
        return headers["Authorization"].removeprefix("Bearer ")

    def enter(self, phase: str) -> None:
        self.session.env["DATABRICKS_BEARER"] = self.bearer(phase)

    def configure(self, phase: str) -> None:
        self.enter(phase)
        self.recorders[phase].configure_session(self.session, CONFIGURE_ARGS)

    def configure_in_terminal(self, phase: str) -> None:
        """A terminal launch made ug own machine-wide files; only a terminal configure rewrites them."""
        self.enter(phase)
        recorder = self.recorders[phase]
        args = [*CONFIGURE_ARGS, "--workspace", recorder.upstream]
        with TerminalProcess(
            self.session, "ug", [str(self.session.binary), *args], f"phase-{phase}-configure"
        ) as terminal:
            terminal.finish(timeout=300)
        recorder.retarget_session(self.home)

    def agent_binary(self, agent: str) -> str:
        return shutil.which(agent, path=self.session.env["PATH"])

    def mcp_list(self, agent: str) -> tuple[str, ...]:
        """Effective registrations as each agent reports them."""
        if agent == CODEX:
            output = self.session.run("mcp", "list", "--json", binary=self.agent_binary(agent))
            return tuple(server["name"] for server in json.loads(output.stdout))
        output = self.session.run("mcp", "list", binary=self.agent_binary(agent)).stdout
        # `name: <command or url> - <status>`; prose lines contain spaces before the colon.
        names = []
        for line in output.splitlines():
            name, separator, _ = line.strip().partition(":")
            if separator and name and " " not in name:
                names.append(name)
        return tuple(names)

    def skill_files(self) -> dict[str, str]:
        files = {}
        for root in (self.home / ".claude/skills", self.home / ".agents/skills"):
            for path in sorted(root.rglob("*")) if root.is_dir() else []:
                if path.is_file():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    files[str(path.relative_to(self.home))] = digest
        return files

    def agent_files(self) -> AgentFiles:
        """Excludes ug's own state and caches and Claude's runtime counters in .claude.json."""
        codex_config = read_toml(self.home / ".codex/config.toml")
        claude_mcp = read_json(self.home / ".claude/.claude.json").get("mcpServers", {})
        return AgentFiles(
            claude_settings=read_json(self.home / ".claude/ucode-settings.json"),
            claude_user_settings=(self.home / ".claude/settings.json").read_text(),
            codex_config=codex_config,
            codex_ucode_config=read_toml(self.home / ".codex/ucode.config.toml"),
            codex_model_catalog=read_json(self.home / ".ucode/codex-model-catalog.json"),
            claude_mcp=claude_mcp,
            codex_mcp=codex_config.get("mcp_servers", {}),
            claude_mcp_list=self.mcp_list(CLAUDE),
            codex_mcp_list=self.mcp_list(CODEX),
            skill_files=self.skill_files(),
            user_mcp_tools={
                CLAUDE: self.user_mcp_tools(claude_mcp.get(USER_MCP)),
                CODEX: self.user_mcp_tools(codex_config.get("mcp_servers", {}).get(USER_MCP)),
            },
        )

    @staticmethod
    def user_mcp_tools(registration: dict | None) -> tuple[str, ...]:
        return mcp_tool_names(registration) if registration else ()

    def inference_requests(self, phase: str, agent: str, checkpoint: int) -> tuple:
        return tuple(
            request
            for request in self.recorders[phase].requests_after(checkpoint)
            if request.path == INFERENCE_PATHS[agent]
        )

    def run_task(self, phase: str, agent: str, *, marker: str | None = None) -> AgentTask:
        recorder = self.recorders[phase]
        recorder.prepare_launch()
        evidence = SessionEvidence(self.home, agent)
        task = FileTask(self.session)
        checkpoint = recorder.checkpoint()
        if agent == CLAUDE:
            env = dict(self.session.env)
            if marker:
                self.session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={marker}"
            try:
                result = self.session.run(
                    CLAUDE,
                    "--",
                    "-p",
                    task.prompt,
                    "--output-format",
                    "json",
                    "--allowedTools",
                    "Read",
                    timeout=240,
                )
            finally:
                self.session.env = env
        elif agent == CODEX:
            overrides = (
                ["--config", f'otel.span_attributes.ug_integration_marker="{marker}"']
                if marker
                else []
            )
            result = self.session.run(
                CODEX,
                "--",
                *overrides,
                "exec",
                "--skip-git-repo-check",
                "--json",
                task.prompt,
                timeout=240,
            )
        else:
            raise ValueError(f"Unsupported agent: {agent!r}")
        task.assert_headless_answer(agent, result)
        turn = evidence.completed(task)
        assert turn, f"No completed native {agent} turn for the task prompt"
        requests = self.inference_requests(phase, agent, checkpoint)
        assert requests, f"The recorder saw no {agent} inference request"
        return AgentTask(requests=requests, turn_models=tuple(turn.models))

    def bare_ug_task(self, phase: str) -> AgentTask:
        """Bare `ug` in a real terminal; helpers.terminal.Terminal only drives `ug <agent>`."""
        recorder = self.recorders[phase]
        recorder.prepare_launch()
        evidence = SessionEvidence(self.home, CODEX)
        task = FileTask(self.session)
        checkpoint = recorder.checkpoint()
        with AgentTerminal(
            self.session, CODEX, [str(self.session.binary)], f"phase-{phase}-bare-ug"
        ) as tui:
            tui.boot()
            tui.submit(task.prompt)

            def completed(screen):
                assert_no_terminal_api_error(screen)
                assert "Do you want to proceed?" not in screen, screen
                return evidence.completed(task) is not None

            tui.wait_for(completed, "completed native file task", timeout=240)
            tui.exit_normally()
        requests = self.inference_requests(phase, CODEX, checkpoint)
        assert requests, "The recorder saw no bare ug Codex inference request"
        return AgentTask(requests=requests, turn_models=tuple(evidence.completed(task).models))

    def trace_count(self, marker: str, *, resource: bool) -> int:
        # Codex puts span attributes on spans; Claude puts OTEL_RESOURCE_ATTRIBUTES on resources.
        attributes = "resource.attributes" if resource else "attributes"
        return query_count(
            self.trace_url,
            self.bearer("a"),
            self.warehouse_id,
            f"SELECT COUNT(*) FROM {self.trace_table} "
            "WHERE time > current_timestamp() - INTERVAL 2 HOURS "
            f"AND variant_get({attributes}, '$[\"ug_integration_marker\"]', 'STRING') = :marker",
            [{"name": "marker", "value": marker, "type": "STRING"}],
        )

    def wait_for_codex_span(self, marker: str) -> int:
        deadline = time.monotonic() + TRACE_DEADLINE_SECONDS
        while (count := self.trace_count(marker, resource=False)) == 0:
            if time.monotonic() > deadline:
                break
            time.sleep(15)
        return count

    def seed_user_state(self) -> SeededUserState:
        """Ordinary user-owned prefs, MCP registration, and skill; no ug state."""
        claude_settings = self.home / ".claude/settings.json"
        claude_settings.parent.mkdir(parents=True)
        claude_settings.write_text(
            json.dumps({"cleanupPeriodDays": 45, "env": {"UG_E2E_USER_PREF": self.run_id}})
        )
        codex_config = self.home / ".codex/config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "hide_agent_reasoning = true\n\n"
            f"[projects.{json.dumps(str(self.session.cwd))}]\n"
            'trust_level = "trusted"\n'
        )
        server = self.home / "user-mcp" / "echo.py"
        server.parent.mkdir()
        server.write_text(USER_MCP_SERVER)
        for agent, args in (
            (CLAUDE, ["mcp", "add", "-s", "user", USER_MCP, "--", sys.executable, str(server)]),
            (CODEX, ["mcp", "add", USER_MCP, "--", sys.executable, str(server)]),
        ):
            self.session.run(*args, binary=self.agent_binary(agent))
        for root in (".claude/skills", ".agents/skills"):
            skill = self.home / root / USER_SKILL / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                f"---\nname: {USER_SKILL}\ndescription: Notes the user wrote by hand.\n---\n\n"
                f"Reply with {self.run_id}.\n"
            )
        assert not (self.home / ".ucode").exists(), "Seeding must not create ug state"
        return SeededUserState(
            claude_settings=claude_settings.read_text(),
            codex_keys=self.user_codex_keys(read_toml(codex_config)),
            mcp_registrations={
                CLAUDE: read_json(self.home / ".claude/.claude.json")["mcpServers"][USER_MCP],
                CODEX: read_toml(codex_config)["mcp_servers"][USER_MCP],
            },
            skill_files=self.skill_files(),
        )

    @staticmethod
    def user_codex_keys(codex_config: dict) -> dict:
        return {key: codex_config.get(key) for key in ("hide_agent_reasoning", "projects")}


class TestCujRepeatedConfig(BaseCujTest):
    WORKSPACE_URL = "https://dbc-8c38ce9b-634b.cloud.databricks.com"
    PHASE_B_URL = "https://dbc-175bc4a3-a511.cloud.databricks.com"
    PHASE_C_URL = "https://dbc-998133ab-de86.cloud.databricks.com"

    @pytest.fixture(scope="class")
    def journey(self, cuj):
        # `cuj` reverts machine-wide files at teardown and checks workspace A is unchanged.
        session, workspace_a, recorder_a = cuj
        workspaces = {
            "a": workspace_a,
            "b": Workspace(workspace_client(self.PHASE_B_URL)),
            "c": Workspace(workspace_client(self.PHASE_C_URL)),
        }
        published = {phase: workspace.config() for phase, workspace in workspaces.items()}
        with (
            TuiRequestRecorder(workspaces["b"].url) as recorder_b,
            TuiRequestRecorder(workspaces["c"].url) as recorder_c,
        ):
            recorders = {"a": recorder_a, "b": recorder_b, "c": recorder_c}
            try:
                yield Journey(session, workspaces, published, recorders)
            finally:
                for phase in ("b", "c"):
                    workspaces[phase].assert_unchanged(published[phase])

    @pytest.fixture(scope="class")
    def phase_a(self, journey):
        # Configure A directly twice: through the recorder, the repeat would be a switch back
        # from the recorder URL rather than a repeat of the same workspace.
        journey.enter("a")
        upstream = ["--workspace", journey.workspaces["a"].url]
        journey.session.configure([*CONFIGURE_ARGS, *upstream])
        first = journey.agent_files()
        journey.session.configure([*CONFIGURE_ARGS, *upstream])
        second = journey.agent_files()
        journey.configure("a")
        return PhaseA(
            first=first,
            second=second,
            claude=journey.run_task("a", CLAUDE),
            codex=journey.run_task("a", CODEX),
        )

    @pytest.fixture(scope="class")
    def phase_b(self, journey, phase_a):
        journey.configure("b")
        files = journey.agent_files()
        claude_marker = f"{journey.run_id}-claude-b"
        codex_marker = f"{journey.run_id}-codex-b"
        claude = journey.run_task("b", CLAUDE, marker=claude_marker)
        codex = journey.run_task("b", CODEX, marker=codex_marker)
        bare_ug = journey.bare_ug_task("b")
        codex_span_count = journey.wait_for_codex_span(codex_marker)
        return PhaseB(
            files=files,
            restarted=journey.agent_files(),
            claude=claude,
            codex=codex,
            bare_ug=bare_ug,
            os_managed_codex=read_toml(CODEX_OS_MANAGED_CONFIG),
            codex_span_count=codex_span_count,
            claude_span_count=journey.trace_count(claude_marker, resource=True),
        )

    @pytest.fixture(scope="class")
    def phase_c(self, journey, phase_b):
        journey.configure_in_terminal("c")
        files = journey.agent_files()
        os_managed_codex = read_toml(CODEX_OS_MANAGED_CONFIG)
        # The terminal configure wrote /etc for C's upstream; a terminal launch must re-own it
        # for the recorder URL before non-interactive launches, which refuse conflicting values.
        bare_ug = journey.bare_ug_task("c")
        recorder = journey.recorders["c"]
        recorder.prepare_launch()
        picker = journey.session.codex_model_ids(
            ["app-server", "--listen", "stdio://"], "phase-c-models"
        )
        checkpoint = recorder.checkpoint()
        disabled = journey.session.run(CLAUDE, "--", "-p", "unused", ok=False)
        claude_requests = journey.inference_requests("c", CLAUDE, checkpoint)
        codex_marker = f"{journey.run_id}-codex-c"
        codex = journey.run_task("c", CODEX, marker=codex_marker)
        final = journey.agent_files()
        # Absence can't be polled; wait out the ingestion window before counting.
        time.sleep(TRACE_INGESTION_SECONDS)
        return PhaseC(
            files=files,
            final=final,
            os_managed_codex=os_managed_codex,
            codex=codex,
            picker=tuple(picker),
            disabled_claude_returncode=disabled.returncode,
            disabled_claude_output=disabled.stdout + disabled.stderr,
            disabled_claude_requests=len(claude_requests),
            bare_ug=bare_ug,
            phase_b_span_count=journey.trace_count(f"{journey.run_id}-codex-b", resource=False),
            codex_span_count=journey.trace_count(codex_marker, resource=False),
        )

    # Phase A: CUJ 1's config plus a named managed MCP.

    def test_phase_a_workspace_publishes_plan_config(self, journey):
        config = journey.published["a"]
        claude = agent_config(config, CodingAgent.CLAUDE_CODE)
        codex = agent_config(config, CodingAgent.CODEX)
        assert config["default_agent"] == CodingAgent.CLAUDE_CODE
        assert claude["models"]["model_services"] == list(CLAUDE_STATIC_MODELS)
        assert codex["models"]["model_services"] == [CODEX_SOL, CODEX_LUNA]
        assert tracing_enabled(claude) and tracing_enabled(codex)
        assert config["mcp_servers"]["names"] == [FIXTURE_READER_MCP_SERVICE_NAME]

    def test_phase_a_repeat_configure_changes_nothing(self, phase_a):
        assert phase_a.second == phase_a.first

    def test_phase_a_registers_managed_and_user_mcp_once(self, phase_a):
        files = phase_a.second
        for registrations in (files.claude_mcp, files.codex_mcp):
            assert {MANAGED_MCP_ENTRY, USER_MCP} <= set(registrations)
        for listed in (files.claude_mcp_list, files.codex_mcp_list):
            assert {MANAGED_MCP_ENTRY, USER_MCP} <= set(listed)
            assert len(listed) == len(set(listed)), listed

    def test_phase_a_claude_sends_claude_headers(self, phase_a):
        for request in phase_a.claude.requests:
            assert request.headers.get(RUN_HEADER) == "cuj6-phase-a"
            assert request.headers.get(AGENT_HEADER) == "claude"

    def test_phase_a_codex_sends_codex_headers(self, phase_a):
        for request in phase_a.codex.requests:
            assert request.headers.get(RUN_HEADER) == "cuj6-phase-a"
            assert request.headers.get(AGENT_HEADER) == "codex"

    def test_phase_a_claude_runs_its_default(self, phase_a):
        assert CLAUDE_DEFAULT in {canonical_model(m) for m in phase_a.claude.request_models}
        assert {canonical_model(m) for m in phase_a.claude.turn_models} == {CLAUDE_DEFAULT}

    def test_phase_a_codex_runs_its_default(self, phase_a):
        assert {canonical_model(m) for m in phase_a.codex.request_models} == {CODEX_SOL}
        assert {canonical_model(m) for m in phase_a.codex.turn_models} == {CODEX_SOL}

    # Phase B: Claude on an MPS, Codex on UC discovery; headers, MCP, and tracing changed.

    def test_phase_b_workspace_publishes_plan_config(self, journey):
        config = journey.published["b"]
        claude = agent_config(config, CodingAgent.CLAUDE_CODE)
        codex = agent_config(config, CodingAgent.CODEX)
        assert config["default_agent"] == CodingAgent.CODEX
        assert claude["models"] == {"model_provider_service": CLAUDE_PROVIDER}
        assert claude["default_models"]["default_model"] == CLAUDE_PROVIDER_MODEL
        assert not tracing_enabled(claude) and not claude.get("http_headers")
        assert codex["models"] == {"unity_catalog_location": CODEX_MODEL_LOCATION}
        assert codex["default_models"]["default_model"] == CODEX_UC_MODEL
        assert lower_keys(codex["http_headers"]) == {RUN_HEADER: "cuj6-phase-b"}
        assert tracing_enabled(codex)
        assert not config.get("mcp_servers", {}).get("names")

    def test_phase_b_claude_routes_through_the_provider(self, phase_b):
        assert CLAUDE_PROVIDER_MODEL in phase_b.claude.request_models
        leaf = model_leaf(CLAUDE_PROVIDER_MODEL)
        assert all(leaf in model for model in phase_b.claude.turn_models), phase_b.claude
        for request in phase_b.claude.requests:
            assert request.headers.get(PROVIDER_HEADER) == CLAUDE_PROVIDER

    def test_phase_b_claude_drops_phase_a_headers(self, phase_b):
        for request in phase_b.claude.requests:
            assert not {RUN_HEADER, AGENT_HEADER} & set(request.headers)

    def test_phase_b_codex_routes_through_uc_discovery(self, phase_b):
        assert phase_b.codex.request_models == {CODEX_UC_MODEL}
        assert set(phase_b.codex.turn_models) == {CODEX_UC_MODEL}
        for request in phase_b.codex.requests:
            assert request.headers.get(PARENT_SCHEMA_HEADER) == CODEX_MODEL_LOCATION

    def test_phase_b_codex_sends_only_the_phase_b_header(self, phase_b):
        for request in phase_b.codex.requests:
            assert request.headers.get(RUN_HEADER) == "cuj6-phase-b"
            assert AGENT_HEADER not in request.headers

    def test_phase_b_bare_ug_launches_codex_on_its_default(self, phase_b):
        assert phase_b.bare_ug.request_models == {CODEX_UC_MODEL}
        assert set(phase_b.bare_ug.turn_models) == {CODEX_UC_MODEL}
        for request in phase_b.bare_ug.requests:
            assert request.headers.get(RUN_HEADER) == "cuj6-phase-b"

    def test_phase_b_terminal_launch_writes_phase_b_machine_wide_headers(self, phase_b):
        provider = phase_b.os_managed_codex["model_providers"]["Databricks"]
        assert lower_keys(provider["http_headers"]).get(RUN_HEADER) == "cuj6-phase-b"

    def test_phase_b_drops_the_static_claude_picker(self, phase_b):
        settings = phase_b.files.claude_settings
        assert not {"availableModels", "enforceAvailableModels"} & set(settings)
        picker = json.dumps(settings.get("modelPicker", {}))
        assert not [model for model in CLAUDE_STATIC_MODELS if model in picker], picker

    def test_phase_b_drops_claude_family_defaults(self, phase_b):
        stale = {
            key: value
            for key, value in phase_b.files.claude_env.items()
            if key.startswith("ANTHROPIC_DEFAULT_")
            and value.removesuffix("[1m]") in CLAUDE_STATIC_MODELS
        }
        assert not stale

    def test_phase_b_drops_claude_tracing_settings(self, phase_b):
        assert "otelHeadersHelper" not in phase_b.files.claude_settings
        assert not set(CLAUDE_OTEL_TRACE_ENV_KEYS) & set(phase_b.files.claude_env)

    def test_phase_b_drops_the_static_codex_catalog(self, phase_b):
        assert "model_catalog_json" not in phase_b.files.codex_config

    def test_phase_b_managed_mcp_stays_removed_across_launches(self, phase_b):
        for files in (phase_b.files, phase_b.restarted):
            for registered in (
                files.claude_mcp,
                files.codex_mcp,
                files.claude_mcp_list,
                files.codex_mcp_list,
            ):
                assert MANAGED_MCP_ENTRY not in registered

    def test_phase_b_traces_codex_but_not_claude(self, phase_b):
        # Codex's span proves the destination is up before Claude's absence counts.
        assert phase_b.codex_span_count > 0
        assert phase_b.claude_span_count == 0

    # Phase C: Claude disabled, Codex on a Luna-only static list without headers or tracing.

    def test_phase_c_workspace_publishes_plan_config(self, journey):
        config = journey.published["c"]
        assert [entry["agent"] for entry in config["enabled_agents"]] == [CodingAgent.CODEX]
        codex = agent_config(config, CodingAgent.CODEX)
        assert codex["models"] == {"model_services": [CODEX_LUNA]}
        assert codex["default_models"]["default_model"] == CODEX_LUNA
        assert not tracing_enabled(codex) and not codex.get("http_headers")

    def test_phase_c_terminal_configure_clears_phase_b_machine_wide_headers(self, phase_c):
        provider = phase_c.os_managed_codex["model_providers"]["Databricks"]
        stale = {RUN_HEADER, AGENT_HEADER, PARENT_SCHEMA_HEADER}
        assert not stale & set(lower_keys(provider.get("http_headers", {})))

    def test_phase_c_codex_drops_phase_b_headers(self, phase_c):
        for request in phase_c.codex.requests:
            assert not {RUN_HEADER, AGENT_HEADER, PARENT_SCHEMA_HEADER} & set(request.headers)

    def test_phase_c_codex_picker_is_only_luna(self, phase_c):
        assert phase_c.picker == (CODEX_LUNA,)

    def test_phase_c_codex_and_bare_ug_run_luna(self, phase_c):
        assert {canonical_model(m) for m in phase_c.codex.request_models} == {CODEX_LUNA}
        assert {canonical_model(m) for m in phase_c.codex.turn_models} == {CODEX_LUNA}
        assert {canonical_model(m) for m in phase_c.bare_ug.request_models} == {CODEX_LUNA}
        assert {canonical_model(m) for m in phase_c.bare_ug.turn_models} == {CODEX_LUNA}

    def test_phase_c_claude_is_rejected_before_inference(self, phase_c):
        assert phase_c.disabled_claude_returncode != 0
        assert "doesn't enable Claude Code" in phase_c.disabled_claude_output
        assert phase_c.disabled_claude_requests == 0

    def test_phase_c_codex_exports_no_span(self, phase_c):
        # Phase B's span is still queryable, so a zero below is not a query outage.
        assert phase_c.phase_b_span_count > 0
        assert phase_c.codex_span_count == 0

    # Every phase: the user's own settings, MCP, and skill survive.

    def test_user_state_survives_every_phase(self, journey, phase_a, phase_b, phase_c):
        seeded = journey.seeded
        for files in (phase_a.second, phase_b.restarted, phase_c.final):
            assert files.claude_user_settings == seeded.claude_settings
            assert journey.user_codex_keys(files.codex_config) == seeded.codex_keys
            assert files.claude_mcp.get(USER_MCP) == seeded.mcp_registrations[CLAUDE]
            assert files.codex_mcp.get(USER_MCP) == seeded.mcp_registrations[CODEX]
            assert USER_MCP in files.claude_mcp_list and USER_MCP in files.codex_mcp_list
            for path, digest in seeded.skill_files.items():
                assert files.skill_files.get(path) == digest, path
            assert files.user_mcp_tools == {CLAUDE: ("echo",), CODEX: ("echo",)}
        health = journey.session.run(
            "mcp", "get", USER_MCP, binary=journey.agent_binary(CLAUDE)
        ).stdout
        assert "Connected" in health, health
