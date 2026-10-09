from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, MutableMapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NoReturn, TextIO

from ucode import config_io
from ucode.codex_config import (
    codex_config_args,
    custom_catalog_models,
    custom_catalog_path,
)
from ucode.config_io import (
    APP_DIR,
    read_json_safe,
    write_json_file,
    write_text_file,
)
from ucode.constants import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    LOOPBACK_HOST,
    SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
    SMART_ROUTING_ENV_KEYS,
)
from ucode.custom_oauth import custom_oauth_cli_enabled, get_custom_client_token
from ucode.databricks import (
    AnthropicModelCatalog,
    build_auth_token_argv,
    get_databricks_token,
    list_anthropic_model_catalog,
    list_anthropic_models,
)
from ucode.launcher import exec_or_spawn
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)
from ucode.skills import SMART_ROUTER_SKILL, install_skill
from ucode.smart_routing import claude_routing, codex_interposer, orchestrator, routing
from ucode.smart_routing.claude_hooks import (
    FIRST_PROMPT_SOCKET_ENV,
    sync_first_prompt_hook,
    sync_smart_routing_hooks,
)
from ucode.smart_routing.codex_hooks import merge_pre_tool_use_hooks, routing_models
from ucode.smart_routing.config import apply_config, resolve_environment
from ucode.smart_routing.session_env import SESSION_ENV_VAR, SESSION_PYTHON_ENV_VAR, start_session
from ucode.ui import print_warning

LEGACY_STATE_KEY = "smart_routing_enabled"

CODEX_INTERPOSER_LOG = APP_DIR / "codex-v2-interposer.log"

CLAUDE_TARGET_MODEL = "system.ai.claude-sonnet-4-6[1m]"  # TODO(lilly): replace with smart router.
CLAUDE_PTY_LOG = APP_DIR / "claude-v2-pty.log"

APP_SERVER_READY_TIMEOUT_SECONDS = 30
PROCESS_SHUTDOWN_TIMEOUT_SECONDS = 5
OAUTH_TOKEN_ENV_VAR = "OAUTH_TOKEN"
HEALTH_REQUEST_TIMEOUT_SECONDS = 1
HEALTH_POLL_INTERVAL_SECONDS = 0.25
CLAUDE_ROUTE_SELECTION_TIMEOUT_S = 20.0
CLAUDE_ROUTED_AGENT_PREFIX = "ucode-route-"
CLAUDE_ROUTING_PLUGIN_NAME = "ug-smart-router"
CLAUDE_ROUTED_AGENT_PROMPT = (
    "Complete the delegated task exactly as requested. Follow the parent agent's instructions and "
    "return a concise report of your findings or changes."
)


class ClaudeRoutingSetupError(RuntimeError):
    """Routing files could not be written; the caller can launch Claude normally."""


def _prepare_smart_router_session(agent: str) -> Path:
    try:
        install_skill(SMART_ROUTER_SKILL, agent, config_io.APP_DIR.parent)
    except (OSError, RuntimeError) as exc:
        print_warning(f"Could not install the {SMART_ROUTER_SKILL} skill: {exc}")
    return start_session()


def _launch_token(state: dict, workspace: str) -> str:
    custom_oauth = state.get("custom_oauth")
    if custom_oauth_cli_enabled(custom_oauth) and isinstance(custom_oauth, dict):
        return get_custom_client_token(
            workspace,
            custom_oauth["client_id"],
            custom_oauth["redirect_url"],
            scopes=custom_oauth["scopes"],
            profile=custom_oauth.get("profile"),
        )
    return get_databricks_token(workspace, state.get("profile"))


def _model_picker_catalog() -> AnthropicModelCatalog | None:
    """Read model-picker rows using the managed-settings then ucode-settings waterfall.

    A managed picker is authoritative for smart routing: its rows are the models the
    administrator exposed, so there is no need to query the gateway catalog first.
    """
    try:
        from ucode.agents.claude import (
            CLAUDE_SETTINGS_PATH,
            CLAUDE_USER_SETTINGS_PATH,
            _managed_settings_path,
        )

        # Hierarchy: managed settings, CLI-supplied settings (ucode-settings.json), local user
        # settings, based on the modelPicker scope documented at https://code.claude.com/docs/en/settings-reference#modelpicker.
        paths = [_managed_settings_path(), CLAUDE_SETTINGS_PATH, CLAUDE_USER_SETTINGS_PATH]
    except (ImportError, OSError):
        return None
    for path in paths:
        if path is None or not path.is_file():
            continue
        settings = read_json_safe(path)
        picker_settings = settings.get("modelPicker") if isinstance(settings, dict) else None
        picker = picker_settings.get("options") if isinstance(picker_settings, dict) else None
        if not isinstance(picker, list):
            continue
        model_ids: list[str] = []
        seen: set[str] = set()
        for row in picker:
            if not isinstance(row, dict) or not isinstance(row.get("model"), str):
                continue
            model_id = row["model"].strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            model_ids.append(model_id)
        if model_ids:
            return AnthropicModelCatalog(model_ids, {})
    return None


def smart_routing_enabled(
    env: MutableMapping[str, str] | None, *, default: bool, agent: str
) -> bool:
    source = resolve_environment(env, agent=agent)
    values = [
        source.get(var) for var in (ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR)
    ]
    if "1" in values:
        return True
    if "0" in values:
        return False
    return default


def first_prompt_routing_enabled(
    env: MutableMapping[str, str] | None = None, *, agent: str
) -> bool:
    """Whether the first prompt is routed. Subagent-only wins over the full V2 flag."""
    source = resolve_environment(env, agent=agent)
    return (
        source.get(ENABLE_SMART_ROUTING_ENV_VAR) == "1"
        and source.get(ENABLE_SUBAGENT_ROUTING_ENV_VAR) != "1"
    )


def enable_smart_routing(
    env: MutableMapping[str, str] | None = None, *, agent: str
) -> dict[str, str | None]:
    """Set the full smart-routing env var and return the prior value of every routing var."""
    return override_smart_routing(True, env, agent=agent)


def override_smart_routing(
    enabled: bool,
    env: MutableMapping[str, str] | None = None,
    *,
    agent: str,
) -> dict[str, str | None]:
    """Set an explicit launch-scoped routing choice and return the prior values."""
    target = os.environ if env is None else env
    previous = {var: target.get(var) for var in SMART_ROUTING_ENV_KEYS}
    previous.update(apply_config(target, agent=agent))
    if enabled:
        target[ENABLE_SMART_ROUTING_ENV_VAR] = "1"
    else:
        target.update(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
    return previous


def restore_smart_routing_env(
    previous: dict[str, str | None], env: MutableMapping[str, str] | None = None
) -> None:
    """Restore the env state captured when smart routing was enabled or disabled."""
    target = os.environ if env is None else env
    for var, value in previous.items():
        if value is None:
            target.pop(var, None)
        else:
            target[var] = value


def disable_smart_routing(
    env: MutableMapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Temporarily remove the smart-routing env vars and return their prior values."""
    target = os.environ if env is None else env
    previous = {var: target.pop(var, None) for var in SMART_ROUTING_ENV_KEYS}
    if SMART_ROUTER_CONFIG_VERSION_ENV_VAR in target:
        previous[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = target.pop(
            SMART_ROUTER_CONFIG_VERSION_ENV_VAR
        )
    return previous


def _loopback_websocket_url(port: int) -> str:
    return f"ws://{LOOPBACK_HOST}:{port}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK_HOST, 0))
        return sock.getsockname()[1]


def _wait_for_app_server(port: int, timeout: float) -> bool:
    url = f"http://{LOOPBACK_HOST}:{port}/healthz"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(  # noqa: S310
                url, timeout=HEALTH_REQUEST_TIMEOUT_SECONDS
            ) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(HEALTH_POLL_INTERVAL_SECONDS)
    return False


def _switch_message(model: str, reason: str) -> str:
    return routing.format_switch_message(model, reason)


def format_routing_notice(model: str, reason: str | None) -> str:
    return routing.format_switch_message(model, reason or "")


def _canonical_claude_model_id(model: str) -> str:
    """Use the system.ai id when Anthropic discovery returns a legacy alias."""
    prefix = "databricks-claude-"
    if model.startswith(prefix):
        return f"system.ai.claude-{model[len(prefix) :]}"
    return model


def _canonical_claude_models(model_ids: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            _canonical_claude_model_id(model)
            for model in model_ids
            if isinstance(model, str) and model
        )
    )


def _unwrapped_claude_model_id(model: str) -> str:
    """Strip the Anthropic gateway wrapper, preserving the embedded model id."""
    return routing.unwrap_anthropic_gateway_model(model)


def _claude_router_model_id(model: str) -> str:
    """Unwrap an Anthropic gateway id, then apply standard model normalization."""
    return routing.normalize_model(_unwrapped_claude_model_id(model))


def _claude_model_overrides(model_ids: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    prefix = "system.ai."
    for model in _canonical_claude_models(model_ids):
        if model.startswith(f"{prefix}claude-"):
            overrides[model[len(prefix) :]] = model
    return overrides


def _routed_claude_agent_slug(model: str) -> str:
    canonical = _canonical_claude_model_id(model)
    normalized = routing.normalize_model(canonical)
    safe = "".join(character if character.isalnum() else "-" for character in normalized)
    slug = "-".join(part for part in safe.split("-") if part)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:8]
    return f"{CLAUDE_ROUTED_AGENT_PREFIX}{slug[:36]}-{digest}"


def _routed_claude_agent_name(model: str) -> str:
    return f"{CLAUDE_ROUTING_PLUGIN_NAME}:{_routed_claude_agent_slug(model)}"


def _routed_claude_agent_definitions(model_ids: list[str]) -> dict[str, dict[str, str]]:
    return {
        _routed_claude_agent_name(model): {
            "description": f"Smart-routed coding agent using {model}",
            "prompt": CLAUDE_ROUTED_AGENT_PROMPT,
            "model": model,
        }
        for model in _canonical_claude_models(model_ids)
    }


def _write_routed_claude_plugin(plugin_dir: Path, model_ids: list[str]) -> None:
    """Write exact-model agents for launch-scoped loading through --plugin-dir."""
    write_json_file(
        plugin_dir / ".claude-plugin" / "plugin.json",
        {
            "name": CLAUDE_ROUTING_PLUGIN_NAME,
            "version": "1.0.0",
            "description": "Launch-scoped agents for Unity Gateway smart routing.",
            "author": {"name": "Databricks"},
        },
    )
    for name, definition in _routed_claude_agent_definitions(model_ids).items():
        slug = name.partition(":")[2]
        write_text_file(
            plugin_dir / "agents" / f"{slug}.md",
            "\n".join(
                [
                    "---",
                    f"name: {json.dumps(slug)}",
                    f"description: {json.dumps(definition['description'])}",
                    f"model: {json.dumps(definition['model'])}",
                    "---",
                    "",
                    definition["prompt"],
                    "",
                ]
            ),
        )
    orchestrator.add_claude_agents(plugin_dir)
    source = Path(__file__).parents[1] / "agents" / "claude_mods" / "register.ts"
    write_text_file(plugin_dir / "hooks" / "register.ts", source.read_text(encoding="utf-8"))
    write_json_file(plugin_dir / "hooks" / "hooks.json", {"modules": ["./register.ts"]})


def _request_claude_routing_decision(
    workspace: str,
    token: str,
    prompt: str,
    model_ids: list[str],
) -> tuple[routing.RoutingDecision | None, str | None]:
    available: dict[str, str] = {}
    for model in _canonical_claude_models(model_ids):
        available.setdefault(_claude_router_model_id(model), model)
    if not available:
        return None, "Anthropic models endpoint returned no Claude models"
    route_options = [(model, "claude") for model in available]
    return routing.select_route(
        workspace,
        token,
        prompt,
        route_options,
        lambda selected: available.get(_claude_router_model_id(selected)),
        router_name=routing.configured_router_name(),
        timeout=CLAUDE_ROUTE_SELECTION_TIMEOUT_S,
    )


def _route_claude_prompt(
    state: dict,
    token: str,
    prompt: str,
    model_ids: list[str] | None = None,
) -> routing.RoutingDecision:
    workspace = state.get("workspace")
    if not isinstance(workspace, str):
        raise RuntimeError("workspace metadata is unavailable")

    if model_ids is None:
        model_ids, discovery_error = list_anthropic_models(workspace, token)
        if not model_ids:
            raise RuntimeError(
                discovery_error or "Anthropic models endpoint returned no Claude models"
            )
    decision, error = _request_claude_routing_decision(workspace, token, prompt, model_ids)
    if decision is None:
        raise RuntimeError(error or "router returned no Claude model selection")
    return decision


def route_claude_pre_tool_use(
    payload: dict,
    *,
    workspace: str,
    token: str,
    available_models: list[str],
    audit_decision: bool = False,
) -> dict | None:
    """Route a Claude Agent call through a transient exact-model agent definition."""
    route = routing.resolve_spawn_route(
        payload,
        is_spawn_agent=claude_routing.is_spawn_agent_tool,
        decision_fn=lambda task: _request_claude_routing_decision(
            workspace, token, task, available_models
        ),
        default_task_label="Claude Code subagent task",
        model_id_mapper=lambda model: model,
    )
    if route is None:
        return None
    if audit_decision:
        routing.write_decision_record(
            claude_routing.DECISIONS_PATH,
            payload,
            route.task,
            route.decision,
            route.routed_model,
        )
    agent_name = claude_routing.SUBAGENT_NOTICE_CONFIG.name(route.tool_input) or "subagent"
    if orchestrator.enabled(agent=AGENT_CLAUDE):
        agent_name += " [orchestrator on]"
    routing_message = claude_routing.SUBAGENT_NOTICE_CONFIG.message(
        route.decision,
        route.routed_model,
        {**route.tool_input, "subagent_type": agent_name},
    )
    updated_input = {
        **{key: value for key, value in route.tool_input.items() if key != "model"},
        "subagent_type": _routed_claude_agent_name(route.routed_model),
    }
    hook_output = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": updated_input,
        "permissionDecisionReason": routing_message,
    }
    return {"systemMessage": routing_message, "hookSpecificOutput": hook_output}


def _is_claude_target_model(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.removesuffix("[1m]") == CLAUDE_TARGET_MODEL.removesuffix("[1m]")


class _ClaudeModelSettingGuard:
    def __init__(self, settings_path: Path) -> None:
        self.settings_path = settings_path
        self._before: dict | None = None
        self._routed_model: str | None = None
        self._lock: TextIO | None = None

    def begin(self, routed_model: str) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = open(APP_DIR / "claude-v2-model.lock", "a+", encoding="utf-8")
        acquire_exclusive_file_lock(self._lock)
        self._before = read_json_safe(self.settings_path)
        self._routed_model = routed_model

    def is_routed(self) -> bool:
        value = read_json_safe(self.settings_path).get("model")
        return isinstance(value, str) and value == self._routed_model

    def restore(self) -> None:
        if self._before is None:
            return
        try:
            current = read_json_safe(self.settings_path)
            if "model" in self._before:
                current["model"] = self._before["model"]
            else:
                current.pop("model", None)
            write_json_file(self.settings_path, current)
            self._before = None
            self._routed_model = None
        finally:
            if self._lock is not None:
                release_file_lock(self._lock)
                self._lock.close()
                self._lock = None


def launch_claude(
    state: dict,
    tool_args: list[str],
    *,
    binary: str,
    user_settings_path: Path,
    launch_model: str | None,
    compose_settings: Callable[[list[str]], tuple[dict, list[str]]],
    launch_model_args: Callable[[list[str], str | None], list[str]],
    model_name: Callable[[str], str],
) -> NoReturn:
    """Launch Claude in the first-prompt routing PTY wrapper."""
    from ucode.agents.claude import GATEWAY_MODEL_DISCOVERY_ENV_VAR

    if os.name != "nt":
        from ucode.smart_routing import claude_pty

    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError(
            "Smart routing needs a configured workspace; run `ucode configure claude` first."
        )
    token = _launch_token(state, workspace)
    os.environ[OAUTH_TOKEN_ENV_VAR] = token
    # if modelPicker is defined, then skip model discovery.
    picker_catalog = _model_picker_catalog()
    if picker_catalog is None:
        os.environ[GATEWAY_MODEL_DISCOVERY_ENV_VAR] = "1"
        os.environ["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
        catalog = list_anthropic_model_catalog(workspace, token)
    else:
        catalog = picker_catalog
    if not catalog.model_ids:
        raise RuntimeError(
            catalog.error_msg or "Anthropic models endpoint returned no Claude models"
        )
    model_ids = catalog.model_ids

    route_first_prompt = first_prompt_routing_enabled(agent=AGENT_CLAUDE)
    # TODO: Restore first-prompt routing on Windows after replacing the Unix-only PTY wrapper:
    # https://databricks.atlassian.net/browse/AIGTWY-4385
    if route_first_prompt and os.name == "nt":
        print_warning(
            "Claude first-prompt smart routing is unavailable on Windows; using subagent-only "
            "routing."
        )
        route_first_prompt = False
    settings, remaining = compose_settings(tool_args)
    hook_executable = build_auth_token_argv(
        workspace, state.get("profile"), use_pat=bool(state.get("use_pat"))
    )[0]
    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        raise RuntimeError("Claude settings 'env' must be an object for smart routing.")
    env.pop("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", None)
    env[ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR] = (
        "1" if orchestrator.feature_enabled(agent=AGENT_CLAUDE) else "0"
    )
    if route_first_prompt:
        env[ENABLE_SMART_ROUTING_ENV_VAR] = "1"
    else:
        env[ENABLE_SUBAGENT_ROUTING_ENV_VAR] = "1"
    model_overrides = settings.setdefault("modelOverrides", {})
    if not isinstance(model_overrides, dict):
        raise RuntimeError("Claude settings 'modelOverrides' must be an object for smart routing.")
    model_overrides.update(_claude_model_overrides(model_ids))
    routing_state = {
        **state,
        "claude_models": {str(index): model for index, model in enumerate(model_ids)},
    }
    sync_smart_routing_hooks(settings, routing_state, enabled=True)
    if route_first_prompt:
        sync_first_prompt_hook(settings, hook_executable)
    model_setting = _ClaudeModelSettingGuard(user_settings_path)

    def route_prompt(prompt: str) -> claude_pty.FirstPromptRoute:
        decision = _route_claude_prompt(state, token, prompt, model_ids)
        return claude_pty.FirstPromptRoute(
            model=model_name(_unwrapped_claude_model_id(decision.model)),
            display_model=catalog.model_id_to_display_name.get(decision.model, decision.model),
            rationale=decision.rationale,
        )

    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="claude-v2-", dir=APP_DIR) as directory:
            launch_dir = Path(directory)
            settings_path = launch_dir / "settings.json"
            socket_path = launch_dir / "first.sock"
            plugin_dir = launch_dir / "plugin"
            if route_first_prompt:
                env[FIRST_PROMPT_SOCKET_ENV] = str(socket_path)
            session_path = _prepare_smart_router_session(AGENT_CLAUDE)
            env[SESSION_ENV_VAR] = str(session_path)
            env[SESSION_PYTHON_ENV_VAR] = os.environ[SESSION_PYTHON_ENV_VAR]
            orchestrator.sync_launch_config(settings, agent=AGENT_CLAUDE, session_path=session_path)
            try:
                write_json_file(settings_path, settings)
                _write_routed_claude_plugin(plugin_dir, model_ids)
            except Exception as exc:  # noqa: BLE001 - optional setup must not block normal launch
                raise ClaudeRoutingSetupError("Failed to write Claude smart-routing files") from exc
            model_args = launch_model_args(remaining, launch_model)
            argv = [
                binary,
                "--settings",
                str(settings_path),
                *model_args,
                "--plugin-dir",
                str(plugin_dir),
                *remaining,
            ]
            if route_first_prompt:
                returncode = claude_pty.run_claude_pty(
                    argv,
                    route_prompt=route_prompt,
                    socket_path=socket_path,
                    prepare_model_switch=model_setting.begin,
                    model_switch_persisted=model_setting.is_routed,
                    restore_model_setting=model_setting.restore,
                    log_path=CLAUDE_PTY_LOG,
                )
            else:
                proc = subprocess_cross_os.popen(argv)
                try:
                    returncode = proc.wait()
                except KeyboardInterrupt:
                    proc.send_signal(signal.SIGINT)
                    returncode = proc.wait()
    finally:
        model_setting.restore()
    sys.exit(returncode)


# TODO: Replace with /codex/v1/models once /codex/v1/models can send GPT models as well.
def _cached_routing_models(state: dict) -> list[str]:
    """Return the persisted UC model-service ids usable by Codex routing."""
    return routing_models(state)


def _v2_hooks(state: dict, available_models: list[str]) -> dict:
    # Codex combines hook sources itself; copying user hooks here would register them twice.
    doc = {
        "hooks": {
            "PreToolUse": merge_pre_tool_use_hooks([], state, available_models=available_models),
        }
    }
    orchestrator.sync_hooks(doc, agent="codex")
    return doc["hooks"]


def launch_codex(
    state: dict,
    tool_args: list[str],
    *,
    binary: str,
    start_model: str | None,
    render_overlay: Callable[..., dict],
) -> NoReturn:
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError(
            "Smart routing needs a configured workspace; run `ucode configure codex` first."
        )
    if not start_model:
        raise RuntimeError(
            "Smart routing could not determine a starting Codex model for this workspace."
        )

    os.environ[OAUTH_TOKEN_ENV_VAR] = _launch_token(state, workspace)
    catalog_models = custom_catalog_models()
    available_models = catalog_models or _cached_routing_models(state)
    if not available_models:
        print_warning(
            "Smart routing model metadata is unavailable; automatic model switching is unavailable. "
            "Run `ucode configure codex` to enable routing."
        )
    custom_oauth = state.get("custom_oauth")
    overlay = render_overlay(
        workspace,
        start_model,
        state.get("profile"),
        use_pat=bool(state.get("use_pat")),
        custom_oauth=(custom_oauth if custom_oauth_cli_enabled(custom_oauth) else None),
        managed_http_headers=state.get("codex_http_headers"),
    )
    catalog_path = custom_catalog_path()
    if catalog_path is not None:
        overlay["model_catalog_json"] = str(catalog_path)
    overlay["hooks"] = _v2_hooks(state, available_models)
    overlay["features.hooks"] = True
    session_env_path = _prepare_smart_router_session(AGENT_CODEX)
    orchestrator_config = {"hooks": overlay["hooks"]}
    launch_args = orchestrator.codex_launch_args(
        tool_args, orchestrator_config, session_path=session_env_path
    )
    overlay.update(orchestrator_config)
    # Codex constructs tool subprocess environments through its shell policy.
    # Pass both the session marker and its launching interpreter through that policy.
    overlay[f"shell_environment_policy.set.{SESSION_ENV_VAR}"] = str(session_env_path)
    overlay[f"shell_environment_policy.set.{SESSION_PYTHON_ENV_VAR}"] = os.environ[
        SESSION_PYTHON_ENV_VAR
    ]
    config_args = codex_config_args(overlay)
    if not first_prompt_routing_enabled(agent=AGENT_CODEX):
        # Subagent-only routing needs neither the app-server nor the interposer:
        # the hooks ride in the CLI config, so launch the TUI directly.
        exec_or_spawn([binary, *config_args, *launch_args])
    app_port = _free_port()
    app_server_url = _loopback_websocket_url(app_port)

    # Preserve the user's normal CODEX_HOME (including MCP servers, skills, and
    # preferences) and layer only ucode's gateway settings at CLI precedence.
    app_server = subprocess_cross_os.popen(
        [binary, "app-server", *config_args, "--listen", app_server_url],
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stop_interposer = None
    try:
        if not _wait_for_app_server(app_port, timeout=APP_SERVER_READY_TIMEOUT_SECONDS):
            raise RuntimeError(
                "Codex app-server did not become ready for smart routing; check workspace auth."
            )
        tui_port, stop_interposer = codex_interposer.start_interposer_thread(
            LOOPBACK_HOST,
            app_server_url,
            available_models=available_models,
            workspace=workspace,
            token_provider=lambda: _launch_token(state, workspace),
            switch_message_fn=format_routing_notice,
            log_path=CODEX_INTERPOSER_LOG,
        )
        tui_url = _loopback_websocket_url(tui_port)
        provider_args = []
        if os.name == "nt":
            # Windows has no machine-wide Codex config for the remote TUI to inherit.
            provider_args = codex_config_args(
                {
                    key: overlay[key]
                    for key in ("model_provider", "model_providers")
                    if key in overlay
                }
            )
        tui = subprocess_cross_os.popen(
            [binary, *provider_args, "--remote", tui_url, "--model", start_model, *tool_args]
        )
        try:
            returncode = tui.wait()
        except KeyboardInterrupt:
            tui.send_signal(signal.SIGINT)
            returncode = tui.wait()
    finally:
        if stop_interposer is not None:
            stop_interposer()
        app_server.terminate()
        try:
            app_server.wait(timeout=PROCESS_SHUTDOWN_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            app_server.kill()
    sys.exit(returncode)
