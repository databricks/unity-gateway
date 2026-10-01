"""Pi coding agent: writes a ucode-private models.json with Databricks-backed providers.

Pi (https://pi.dev) is a multi-provider coding agent. We register three
providers in its `models.json`, each speaking the API dialect best suited to
that family's gateway path:

- `databricks-claude`  (api: anthropic-messages)       → /ai-gateway/anthropic
- `databricks-openai`  (api: openai-responses)         → /ai-gateway/codex/v1
- `databricks-gemini`  (api: google-generative-ai)     → /ai-gateway/gemini/v1beta

Per-provider `compat` flags work around fields the gateway translators reject:

- claude: `supportsEagerToolInputStreaming: false` — the Anthropic translator
  rejects `tools[].eager_input_streaming` on the streaming + tools path that
  pi uses for every request. With this flag pi omits the per-tool field and
  sends the legacy `anthropic-beta: fine-grained-tool-streaming-...` header
  instead, which the gateway accepts.

OSS / Databricks-foundation models (Llama, Qwen, etc.) are not exposed via
pi today — they live behind /ai-gateway/mlflow/v1 with per-model
`max_tokens` caps that pi has no global way to honor without per-model
config we don't currently maintain.

Each provider's `apiKey` is pi's `!command` config value rather than a baked
bearer, so pi mints one per request via `ug auth-token` and nothing that
expires is written to `models.json` (the on-demand model OpenCode's auth plugin
already uses). A token still reaches the process environment: `launch` exports
`OAUTH_TOKEN` as before.
"""

from __future__ import annotations

import os
import signal

from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    backup_existing_file,
    deep_merge_dict,
    read_json_safe,
    restore_file,
    write_json_file,
)
from ucode.databricks import (
    build_auth_shell_command,
    build_pi_base_urls,
    get_databricks_token,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import mark_tool_managed, save_state
from ucode.telemetry import agent_version, ug_version

from .args import LaunchOptions
from .interface import ConfigureRequest, Install, Models

PI_UCODE_HOME = APP_DIR / "pi-home"
PI_CONFIG_DIR = PI_UCODE_HOME / ".pi" / "agent"
PI_CONFIG_PATH = PI_CONFIG_DIR / "models.json"
PI_SETTINGS_PATH = PI_CONFIG_DIR / "settings.json"
PI_BACKUP_PATH = APP_DIR / "pi-models.backup.json"
PI_SETTINGS_BACKUP_PATH = APP_DIR / "pi-settings.backup.json"

SPEC: ToolSpec = {
    "binary": "pi",
    "package": "@earendil-works/pi-coding-agent",
    "display": "Pi",
    "config_path": PI_CONFIG_PATH,
    "backup_path": PI_BACKUP_PATH,
}

PROVIDER_NAMES = (
    "databricks-claude",
    "databricks-openai",
    "databricks-gemini",
)

PROVIDER_KEYS: list[list[str]] = [["providers", name] for name in PROVIDER_NAMES]

# Old provider names earlier ucode versions wrote; cleaned up on each write so
# users don't end up with stale entries pointing at routes that 400.
LEGACY_PROVIDER_NAMES = ("databricks-anthropic", "databricks-codex", "databricks-oss")


def _resolve_model_selector(
    model: str,
    claude_models: dict[str, str],
    codex_models: list[str],
    gemini_models: list[str],
) -> str:
    """Return a Pi model selector in `<provider>/<model>` form when possible."""
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
    pi_base_urls: dict[str, str],
    claude_models: dict[str, str],
    codex_models: list[str],
    gemini_models: list[str],
) -> tuple[dict, list[list[str]]]:
    """Return (overlay, managed_key_paths) for Pi's private agent config.

    ``api_key`` is a pi config value, not necessarily a literal bearer: see
    ``build_pi_api_key`` for the `!command` form every provider gets."""
    providers: dict = {}
    keys: list[list[str]] = [["model"]]
    # Pi expands header values that match an env var name. Our UA contains
    # `/` and a space so it can never collide — safe to pass as a literal.
    ua_headers = {"User-Agent": f"ucode/{ug_version()} pi/{agent_version('pi')}"}

    claude_ids = sorted(set(claude_models.values()))
    if claude_ids:
        providers["databricks-claude"] = {
            "baseUrl": pi_base_urls["claude"],
            "api": "anthropic-messages",
            "apiKey": api_key,
            "authHeader": True,
            # Gateway's Anthropic translator rejects per-tool
            # `eager_input_streaming` on the streaming + tools path. Pi sends
            # the legacy beta header instead when this is false.
            "compat": {"supportsEagerToolInputStreaming": False},
            "headers": ua_headers,
            "models": [{"id": m} for m in claude_ids],
        }
        keys.append(["providers", "databricks-claude"])
    if codex_models:
        providers["databricks-openai"] = {
            "baseUrl": pi_base_urls["openai"],
            "api": "openai-responses",
            "apiKey": api_key,
            "authHeader": True,
            "headers": ua_headers,
            "models": [{"id": m} for m in codex_models],
        }
        keys.append(["providers", "databricks-openai"])
    if gemini_models:
        providers["databricks-gemini"] = {
            "baseUrl": pi_base_urls["gemini"],
            "api": "google-generative-ai",
            "apiKey": api_key,
            "authHeader": True,
            "headers": ua_headers,
            "models": [{"id": m} for m in gemini_models],
        }
        keys.append(["providers", "databricks-gemini"])
    overlay: dict = {
        "model": _resolve_model_selector(model, claude_models, codex_models, gemini_models),
    }
    if providers:
        overlay["providers"] = providers
    return overlay, keys


def build_pi_api_key(state: dict) -> str:
    """Return the `!command` apiKey value pi resolves before every request.

    Pi runs a leading-`!` config value as a command and uses its stdout, and it
    resolves the provider apiKey per provider request rather than once per
    process, so the token is minted on demand and never lands in the config.

    No `--force-refresh`: pi has no token cache of its own on this path, so
    forcing a mint would round-trip to the workspace every turn. Plain
    `auth-token` serves the CLI's cached token until it nears expiry."""
    return "!" + build_auth_shell_command(
        state["workspace"],
        state.get("profile"),
        use_pat=bool(state.get("use_pat")),
    )


def write_tool_config(
    state: dict,
    model: str,
    token: str | None = None,
) -> tuple[dict, str]:
    backup_existing_file(PI_CONFIG_PATH, PI_BACKUP_PATH)
    if token is None:
        token = get_databricks_token(state["workspace"], state.get("profile"))
    pi_base_urls = state.get("base_urls", {}).get("pi") or build_pi_base_urls(state["workspace"])
    overlay, managed_keys = render_overlay(
        model,
        build_pi_api_key(state),
        pi_base_urls,
        state.get("claude_models") or {},
        state.get("codex_models") or [],
        state.get("gemini_models") or [],
    )
    existing = read_json_safe(PI_CONFIG_PATH)
    providers = existing.get("providers")
    if isinstance(providers, dict):
        for stale in (*PROVIDER_NAMES, *LEGACY_PROVIDER_NAMES):
            providers.pop(stale, None)
    merged = deep_merge_dict(existing, overlay)
    write_json_file(PI_CONFIG_PATH, merged)
    _write_settings(overlay["model"])
    state = mark_tool_managed(state, "pi", managed_keys)
    save_state(state)
    return state, token


def _write_settings(model_selector: str) -> None:
    # Pin defaultProvider/defaultModel in settings.json so Pi doesn't fall
    # through to an env-key-backed provider (e.g. HF_TOKEN exposing
    # huggingface) in `findInitialModel` when no --model is passed.
    provider, _, model_id = model_selector.partition("/")
    if not model_id:
        return
    backup_existing_file(PI_SETTINGS_PATH, PI_SETTINGS_BACKUP_PATH)
    existing = read_json_safe(PI_SETTINGS_PATH)
    merged = deep_merge_dict(existing, {"defaultProvider": provider, "defaultModel": model_id})
    write_json_file(PI_SETTINGS_PATH, merged)


def default_model(state: dict) -> str | None:
    """Prefer Claude opus → sonnet → haiku; fall back to codex, gemini.

    Drawn from workspace-wide discovery, which is Pi's only source of models.
    """
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
        raise RuntimeError("No Pi model is available on this workspace.")
    _, token = write_tool_config(state, model)
    return token


def build_runtime_env(token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OAUTH_TOKEN"] = token
    env["PI_CODING_AGENT_DIR"] = str(PI_CONFIG_DIR)
    return env


def _model_ids(value: object) -> list[str]:
    """Flatten a state model inventory (str, list, or family-keyed dict) into model ids."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    if isinstance(value, dict):
        return [model for models in value.values() for model in _model_ids(models)]
    return []


class PiAgent:
    """Pi as an :class:`~ucode.agents.interface.Agent`."""

    display = str(SPEC["display"])
    install = Install(binary=str(SPEC["binary"]), package=str(SPEC["package"]))
    # Pi has no MCP support today.
    mcp = None

    def models(self, state: dict) -> Models:
        # A managed static list replaces discovery outright; otherwise Pi serves every family.
        available = tuple(
            dict.fromkeys(
                _model_ids(state.get("pi_static_models"))
                or (
                    _model_ids(state.get("claude_models"))
                    + _model_ids(state.get("codex_models"))
                    + _model_ids(state.get("gemini_models"))
                )
            )
        )
        explicit = state.get("pi_default_model")
        if isinstance(explicit, str) and explicit:
            return Models(available, explicit)
        # Pi writes the first resolved model into its config, so that is the starting model.
        return Models(available, available[0] if available else None)

    def configure(self, state: dict, request: ConfigureRequest) -> dict:
        if not request.model:
            raise RuntimeError("A pi model must be selected before configuration.")
        return write_tool_config(state, request.model)[0]

    def launch(self, state: dict, args: list[str], *, options: LaunchOptions) -> None:
        """Launch Pi; it re-resolves its apiKey command per request, so no refresher."""
        token = _configure_launch(state)
        env = build_runtime_env(token)

        proc = subprocess_cross_os.popen([SPEC["binary"], *args], env=env)
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGINT)
            returncode = proc.wait()

        raise SystemExit(returncode)

    def revert(self, state: dict) -> list[tuple[str, str]]:
        managed = bool((state.get("managed_configs") or {}).get("pi"))
        restored = restore_file(PI_CONFIG_PATH, PI_BACKUP_PATH, managed)
        # Pi keeps its gateway providers in a second file next to the config.
        settings_restored = restore_file(PI_SETTINGS_PATH, PI_SETTINGS_BACKUP_PATH, managed)
        return [
            (f"{self.display} config", "restored" if restored else "unchanged"),
            (f"{self.display} settings", "restored" if settings_restored else "unchanged"),
        ]


AGENT = PiAgent()


def validate_cmd(binary: str) -> list[str]:
    return [binary, "--print", "say hi in 5 words or less"]
