"""Oh-my-pi (omp) coding agent: writes a ucode-private models.yml with Databricks-backed providers.

omp (https://omp.sh) is a multi-provider coding agent compatible with Pi's
provider model but configured in YAML. We register three providers in its
`models.yml`, each speaking the API dialect best suited to that family's
gateway path:

- `databricks-claude`  (api: anthropic-messages)       → /ai-gateway/anthropic
- `databricks-openai`  (api: openai-responses)         → /ai-gateway/codex/v1
- `databricks-gemini`  (api: google-generative-ai)     → /ai-gateway/gemini/v1beta

Per-provider `compat` flags work around fields the gateway translators reject:

- claude: `supportsEagerToolInputStreaming: false` — the Anthropic translator
  rejects `tools[].eager_input_streaming` on the streaming + tools path that
  omp uses for every request. With this flag omp omits the per-tool field and
  sends the legacy `anthropic-beta: fine-grained-tool-streaming-...` header
  instead, which the gateway accepts.

The default model is pinned via `modelRoles.default` in omp's `config.yml`
(the role value omp's startup resolution consults before falling back to the
first available model), never in `models.yml` — whose root carries only
`providers` (unknown root keys fail omp's schema validation).

OSS / Databricks-foundation models (Llama, Qwen, etc.) are not exposed via
omp today — they live behind /ai-gateway/mlflow/v1 with per-model
`max_tokens` caps that omp has no global way to honor without per-model
config we don't currently maintain.

Each provider's `apiKey` is an omp `!command` config value rather than a baked
bearer, so omp mints one on demand via `ug auth-token` and re-runs the command
when the gateway answers 401 (the auth-retry path invalidates command-backed
keys); nothing that expires is written to `models.yml`. omp reads no token from
the environment, so `launch` only redirects its agent dir.
"""

from __future__ import annotations

import os
import shlex
import signal

from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    apply_json_mcp_diff,
    backup_existing_file,
    deep_merge_dict,
    read_yaml_safe,
    write_yaml_file,
)
from ucode.databricks import (
    build_auth_token_argv,
    build_pi_base_urls,
    get_databricks_token,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import mark_tool_managed, save_state
from ucode.telemetry import agent_version, ug_version

from .args import LaunchOptions

OMP_UCODE_HOME = APP_DIR / "omp-home"
OMP_AGENT_DIR = OMP_UCODE_HOME / ".omp" / "agent"
OMP_MODELS_PATH = OMP_AGENT_DIR / "models.yml"
OMP_CONFIG_PATH = OMP_AGENT_DIR / "config.yml"
OMP_MCP_PATH = OMP_AGENT_DIR / "mcp.json"
OMP_MODELS_BACKUP_PATH = APP_DIR / "omp-models.backup.yml"
OMP_CONFIG_BACKUP_PATH = APP_DIR / "omp-config.backup.yml"
OMP_MCP_BACKUP_PATH = APP_DIR / "omp-mcp.backup.json"

SPEC: ToolSpec = {
    "binary": "omp",
    "package": "@oh-my-pi/pi-coding-agent",
    "display": "Oh My Pi",
    "config_path": OMP_MODELS_PATH,
    "backup_path": OMP_MODELS_BACKUP_PATH,
}

PROVIDER_NAMES = (
    "databricks-claude",
    "databricks-openai",
    "databricks-gemini",
)

# Old provider names earlier ucode versions wrote; cleaned up on each write so
# users don't end up with stale entries pointing at routes that 400.
LEGACY_PROVIDER_NAMES = ("databricks-anthropic", "databricks-codex", "databricks-oss")


def _resolve_model_selector(
    model: str,
    claude_models: dict[str, str],
    codex_models: list[str],
    gemini_models: list[str],
) -> str:
    """Return an omp model selector in `<provider>/<model>` form when possible."""
    for name in PROVIDER_NAMES:
        if model.startswith(f"{name}/"):
            return model
    if model in claude_models.values():
        return f"databricks-claude/{model}"
    if model in codex_models:
        return f"databricks-openai/{model}"
    if model in gemini_models:
        return f"databricks-gemini/{model}"
    return model


def render_overlay(
    model: str,
    api_key: str,
    omp_base_urls: dict[str, str],
    claude_models: dict[str, str],
    codex_models: list[str],
    gemini_models: list[str],
) -> tuple[dict, list[list[str]]]:
    """Return (overlay, managed_key_paths) for omp's private agent config.

    ``api_key`` is a config value, not a literal bearer: it carries the
    ``!ug auth-token ...`` command omp runs whenever it needs a token.

    The overlay carries only ``providers``: omp's ``models.yml`` schema
    rejects unknown root keys, so the default-model selector is pinned in
    ``config.yml`` by ``_write_default_model`` instead.
    """
    providers: dict = {}
    keys: list[list[str]] = []
    # omp expands header values that match an env var name. Our UA contains
    # `/` and a space so it can never collide — safe to pass as a literal.
    # One fresh dict per provider: sharing a single object would make YAML's
    # dumper emit anchors and aliases into a file users read and edit.
    ua = f"ucode/{ug_version()} omp/{agent_version('omp')}"

    claude_ids = sorted(set(claude_models.values()))
    if claude_ids:
        providers["databricks-claude"] = {
            "baseUrl": omp_base_urls["claude"],
            "api": "anthropic-messages",
            "apiKey": api_key,
            "authHeader": True,
            # Gateway's Anthropic translator rejects per-tool
            # `eager_input_streaming` on the streaming + tools path. omp sends
            # the legacy beta header instead when this is false.
            "compat": {"supportsEagerToolInputStreaming": False},
            "headers": {"User-Agent": ua},
            "models": [{"id": m} for m in claude_ids],
        }
        keys.append(["providers", "databricks-claude"])
    if codex_models:
        providers["databricks-openai"] = {
            "baseUrl": omp_base_urls["openai"],
            "api": "openai-responses",
            "apiKey": api_key,
            "authHeader": True,
            "headers": {"User-Agent": ua},
            "models": [{"id": m} for m in codex_models],
        }
        keys.append(["providers", "databricks-openai"])
    if gemini_models:
        providers["databricks-gemini"] = {
            "baseUrl": omp_base_urls["gemini"],
            "api": "google-generative-ai",
            "apiKey": api_key,
            "authHeader": True,
            "headers": {"User-Agent": ua},
            "models": [{"id": m} for m in gemini_models],
        }
        keys.append(["providers", "databricks-gemini"])
    overlay: dict = {}
    if providers:
        overlay["providers"] = providers
    return overlay, keys


def build_omp_api_key(state: dict) -> str:
    """Return the `!command` apiKey value omp resolves before a provider request.

    omp runs a leading-`!` config value through its embedded shell and uses its
    stdout, and it re-runs the command after a 401 (the auth-retry path
    invalidates command-backed keys), so the token is minted on demand and never
    lands in the config.

    No `--force-refresh`: omp has no token cache of its own on this path, so
    forcing a mint would round-trip to the workspace on every retry. Plain
    `auth-token` serves the CLI's cached token until it nears expiry.

    Always POSIX-quoted: omp runs `!command` values through its embedded shell
    on every OS (bash on Windows as well), which strips the backslashes from a
    cmd.exe-style ``C:\\...\\ug.exe`` path."""
    argv = build_auth_token_argv(
        state["workspace"],
        state.get("profile"),
        use_pat=bool(state.get("use_pat")),
    )
    return "!" + shlex.join(argv)


def write_tool_config(
    state: dict,
    model: str,
    token: str | None = None,
) -> tuple[dict, str]:
    backup_existing_file(OMP_MODELS_PATH, OMP_MODELS_BACKUP_PATH)
    if token is None:
        token = get_databricks_token(state["workspace"], state.get("profile"))
    omp_base_urls = state.get("base_urls", {}).get("omp") or build_pi_base_urls(state["workspace"])
    claude_models = state.get("claude_models") or {}
    codex_models = state.get("codex_models") or []
    gemini_models = state.get("gemini_models") or []
    overlay, managed_keys = render_overlay(
        model,
        build_omp_api_key(state),
        omp_base_urls,
        claude_models,
        codex_models,
        gemini_models,
    )
    existing = read_yaml_safe(OMP_MODELS_PATH)
    providers = existing.get("providers")
    if isinstance(providers, dict):
        for stale in (*PROVIDER_NAMES, *LEGACY_PROVIDER_NAMES):
            providers.pop(stale, None)
    merged = deep_merge_dict(existing, overlay)
    write_yaml_file(OMP_MODELS_PATH, merged)
    _write_default_model(_resolve_model_selector(model, claude_models, codex_models, gemini_models))
    state = mark_tool_managed(state, "omp", managed_keys)
    save_state(state)
    return state, token


def _write_default_model(model_selector: str) -> None:
    # Pin modelRoles.default in config.yml so omp starts on the Databricks
    # model rather than falling through to the first available model (e.g. an
    # env-key-backed provider) in its startup resolution order.
    if "/" not in model_selector:
        return
    backup_existing_file(OMP_CONFIG_PATH, OMP_CONFIG_BACKUP_PATH)
    existing = read_yaml_safe(OMP_CONFIG_PATH)
    merged = deep_merge_dict(existing, {"modelRoles": {"default": model_selector}})
    write_yaml_file(OMP_CONFIG_PATH, merged)


def default_model(state: dict) -> str | None:
    """Prefer Claude opus → sonnet → haiku; fall back to codex, gemini."""
    claude_models = state.get("claude_models") or {}
    for family in ("opus", "sonnet", "haiku"):
        if claude_models.get(family):
            return claude_models[family]
    codex_models = state.get("codex_models") or []
    if codex_models:
        return codex_models[0]
    gemini_models = state.get("gemini_models") or []
    return gemini_models[0] if gemini_models else next(iter(claude_models.values()), None)


def _configure_launch(state: dict) -> str:
    model = default_model(state)
    if not model:
        raise RuntimeError("No Oh My Pi model is available on this workspace.")
    # Writes models.yml/config.yml and mints one token, so broken auth fails here
    # rather than inside the agent's first request.
    _, token = write_tool_config(state, model)
    return token


def build_runtime_env() -> dict[str, str]:
    # omp reads no token from the environment (auth is the `!command` apiKey in
    # models.yml); only redirect its agent dir into the ucode-private home.
    env = os.environ.copy()
    env["PI_CODING_AGENT_DIR"] = str(OMP_AGENT_DIR)
    return env


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    """Launch Oh My Pi; its `!command` apiKey re-mints the token on 401, so no refresher."""
    _configure_launch(state)
    env = build_runtime_env()

    proc = subprocess_cross_os.popen([SPEC["binary"], *tool_args], env=env)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()

    raise SystemExit(returncode)


def validate_cmd(binary: str) -> list[str]:
    return [binary, "--print", "say hi in 5 words or less"]


def build_mcp_server_entry(argv: list[str]) -> dict:
    # omp's stdioServer schema allows only command/args/env/cwd (stdio is the
    # default transport when `command` is present without `url`), so the entry
    # is exactly this — no `type`/`tools` keys like other clients use.
    return {
        "command": argv[0],
        "args": list(argv[1:]),
    }


def write_user_mcp_servers(add: dict[str, dict], remove: set[str]) -> set[str]:
    """Apply ``add``/``remove`` to omp's agent-dir `mcpServers` in a single read-modify-write. Returns the names actually removed."""
    return apply_json_mcp_diff(
        OMP_MCP_PATH, "mcpServers", add, remove, backup_path=OMP_MCP_BACKUP_PATH
    )


def write_mcp_server_config(name: str, argv: list[str]) -> bool:
    # Returns whether an existing entry was replaced; `remove` is passed so the
    # shared diff reports the prior presence in the same read-modify-write.
    return name in write_user_mcp_servers({name: build_mcp_server_entry(argv)}, {name})


def remove_mcp_server_config(name: str) -> bool:
    return name in write_user_mcp_servers({}, {name})
