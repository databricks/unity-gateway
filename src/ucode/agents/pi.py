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

Gateway model ids are not in Pi's built-in catalog, so a bare `{"id": ...}`
entry gets Pi's custom-model defaults: no thinking, text-only input, a 128K
context and 16K output. Each entry therefore declares its limits, image input,
reasoning and a `thinkingLevelMap` of the levels the model accepts.

OSS / Databricks-foundation models (Llama, Qwen, gpt-oss, etc.) are not exposed
via pi today — they live behind /ai-gateway/mlflow/v1 with per-model
`max_tokens` caps that pi has no global way to honor without per-model
config we don't currently maintain. gpt-oss is bucketed with the GPT models by
name but rejects the Responses requests Pi sends, so Pi leaves it out. Claude
models older than 4.5 reject Pi's Anthropic request shape and are left out too.

Each provider's `apiKey` is pi's `!command` config value rather than a baked
bearer, so pi mints one per request via `ug auth-token` and nothing that
expires is written to `models.json` (the on-demand model OpenCode's auth plugin
already uses). A token still reaches the process environment: `launch` exports
`OAUTH_TOKEN` as before.
"""

from __future__ import annotations

import os
import re
import shlex
import signal

from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    backup_existing_file,
    deep_merge_dict,
    read_json_safe,
    write_json_file,
)
from ucode.constants import AI_GATEWAY_REQUEST_TAGS_HEADER
from ucode.databricks import (
    ANTHROPIC_FAMILIES,
    build_auth_token_argv,
    build_pi_base_urls,
    canonical_model_name,
    classify_model_family,
    gemini_model_token_limits,
    get_databricks_token,
    gpt_model_token_limits,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import mark_tool_managed, save_state
from ucode.telemetry import agent_version, ucode_user_agent, ug_version
from ucode.workspace import workspace_headers

from .args import LaunchOptions
from .copilot import claude_model_name, claude_token_limits
from .request_tags import request_tags_header_value

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

# Pi levels a `thinkingLevelMap` can name. An unmapped level up to `high` sends its own name; an
# unmapped `xhigh` or `max` is hidden; a `None` value hides the level. Pi moves a request for a
# hidden level to a supported one (GPT-5 `--thinking off` runs at `minimal`), so it never sends a
# value the model rejects.
ThinkingLevelMap = dict[str, str | None]

# AI Gateway rejects request bodies over 32 MiB. Pi's default image cap (4.5 MiB base64 each) keeps
# lossless screenshots at 1.5-4 MB, so 15-20 of them pass 32 MiB before token-based compaction
# starts (Pi counts each image as 1,200 tokens). A 768 KiB cap makes Pi JPEG-compress dense ones.
_GATEWAY_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_IMAGE_INPUT_LIMITS: dict[str, object] = {
    "maxRequestBytes": _GATEWAY_MAX_REQUEST_BYTES,
    "images": {
        "resize": {
            "maxWidth": 2000,
            "maxHeight": 2000,
            "maxBytes": 768 * 1024,
            "jpegQuality": 80,
        }
    },
}

# Claude older than 4.5 (e.g. Opus 4.1) rejects Pi's Anthropic request shape:
# `cache_control.ephemeral.ttl` and `thinking.enabled.display` are "Extra inputs are not
# permitted" (HTTP 400), and it refuses tool calls even without them. Other agents still list it.
_MIN_CLAUDE_VERSION = (4, 5)
_CLAUDE_VERSION_RE = re.compile(r"^claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d+))?$")


def _claude_version(model_id: str) -> tuple[str, tuple[int, int]] | None:
    """``(family, (major, minor))`` of a Claude id, or None when it doesn't follow the scheme."""
    match = _CLAUDE_VERSION_RE.match(claude_model_name(model_id))
    if not match:
        return None
    family, major, minor = match.groups()
    return family, (int(major), int(minor or 0))


def _serves_claude_model(model_id: str) -> bool:
    parsed = _claude_version(model_id)
    return parsed is None or parsed[1] >= _MIN_CLAUDE_VERSION


def _serves_codex_model(model_id: str) -> bool:
    # gpt-oss shares the GPT bucket by name but 400s on /ai-gateway/codex/v1 ("Open Responses API
    # does not recognize these fields: prompt_cache_retention").
    return not canonical_model_name(model_id).startswith("gpt-oss")


def _claude_thinking_level_map(model_id: str) -> ThinkingLevelMap | None:
    """Levels for Claude models that need adaptive thinking, else None (budget thinking).

    Follows the Databricks reasoning-models doc, then Pi's catalog for undocumented models.
    Opus/Sonnet 4.6+, Fable 5+ and Haiku 5.5+ take adaptive thinking (Haiku 5.5 rejects budget
    thinking with HTTP 400). Opus/Sonnet 5.5 and Fable can't turn thinking off; Opus/Sonnet 5.5
    and Haiku 5.5 have no `minimal`; the gateway rejects `xhigh` and `max` for Haiku 5.5.
    """
    parsed = _claude_version(model_id)
    if parsed is None:
        return None
    family, version = parsed
    if family in ("opus", "sonnet") and version >= (4, 6):
        xhigh = {(4, 7), (4, 8), (5, 0), (5, 5)} if family == "opus" else {(5, 0), (5, 5)}
        level_map: ThinkingLevelMap = {"max": "max"}
        if version in xhigh:
            level_map["xhigh"] = "xhigh"
        if version == (5, 5):
            level_map.update(off=None, minimal=None)
        return level_map
    if family == "fable" and version >= (5, 0):
        level_map = {"off": None, "max": "max"}
        if version in {(5, 0), (5, 1)}:
            level_map["xhigh"] = "xhigh"
        return level_map
    if family == "haiku" and version >= (5, 5):
        return {"minimal": None}
    return None


def _claude_model_entry(model_id: str) -> dict:
    # Copilot's name-based Claude limits: their windows match what the gateway's Anthropic listing
    # reports, and they cost no request at launch.
    context, output = claude_token_limits(model_id) or (200_000, 32_000)
    entry: dict = {
        "id": model_id,
        "reasoning": True,
        "input": ["text", "image"],
        "inputLimits": _IMAGE_INPUT_LIMITS,
        "contextWindow": context,
        "maxTokens": output,
    }
    level_map = _claude_thinking_level_map(model_id)
    if level_map is not None:
        entry["compat"] = {"forceAdaptiveThinking": True}
        entry["thinkingLevelMap"] = level_map
    return entry


_GPT_SOL_TERRA_LUNA = tuple(
    f"gpt-{version}-{tier}" for version in ("6", "6-1", "5-6") for tier in ("sol", "terra", "luna")
)


def _gpt_thinking_level_map(name: str) -> ThinkingLevelMap | None:
    """Levels for a canonical GPT name, or None for a model Pi should send no reasoning.

    Follows the Databricks reasoning-models doc. `off` maps to the Responses API's `none` only
    where the model accepts it: `gpt-5`, `-mini`, `-nano` and the Pro models reject `none` with
    HTTP 400, and without an `off` entry Pi sends `none` whenever thinking is off. No GPT model
    accepts Pi's `minimal` except the original GPT-5 models.
    """
    if name.startswith("gpt-6-astra"):
        return {"off": None, "minimal": None, "xhigh": "xhigh", "max": "max"}
    if name.startswith(_GPT_SOL_TERRA_LUNA):
        return {"off": "none", "minimal": None, "xhigh": "xhigh", "max": "max"}
    if name.startswith(("gpt-5-2-pro", "gpt-5-4-pro", "gpt-5-5-pro")):
        return {"off": None, "minimal": None, "low": None, "xhigh": "xhigh"}
    if name.startswith("gpt-5-pro"):
        return {"off": None, "minimal": None, "low": None, "medium": None}
    if name.startswith("gpt-5-3-codex-spark"):
        return {"off": None, "minimal": None, "xhigh": "xhigh"}
    if name.startswith(("gpt-5-2", "gpt-5-3", "gpt-5-4", "gpt-5-5")):
        return {"off": "none", "minimal": None, "xhigh": "xhigh"}
    if name.startswith("gpt-5-1"):
        return {"off": "none", "minimal": None}
    if name == "gpt-5" or name.startswith("gpt-5-"):
        return {"off": None}
    return None


def _gpt_model_entry(model_id: str) -> dict:
    limits = gpt_model_token_limits(model_id)
    entry: dict = {
        "id": model_id,
        "contextWindow": limits["context"],
        "maxTokens": limits["output"],
    }
    level_map = _gpt_thinking_level_map(canonical_model_name(model_id))
    if level_map is not None:
        entry.update(
            reasoning=True,
            thinkingLevelMap=level_map,
            input=["text", "image"],
            inputLimits=_IMAGE_INPUT_LIMITS,
        )
    return entry


# Gemini levels from the Databricks reasoning-models doc, then Pi's catalog for models the doc
# doesn't list. A `None` value means reasoning with every Pi level through `high`. Models missing
# here (image-generation variants included) get limits only, so Pi sends no thinking config.
_GEMINI_THINKING_LEVEL_MAPS: dict[str, ThinkingLevelMap | None] = {
    # Always reasons; `minimal` and a thinking budget (Pi's off path) are rejected with HTTP 400.
    "gemini-4-argon": {"off": None, "minimal": None},
    "gemini-3-8-flash": {"minimal": None},
    "gemini-3-7-flash": {"minimal": None},
    "gemini-3-6-flash": {"off": None},
    "gemini-3-5-flash": {"off": None},
    "gemini-3-5-flash-lite": {"off": None},
    "gemini-3-1-flash-lite": {"off": None},
    "gemini-3-flash": {"off": None},
    # The doc lists `minimal` for 3.1 Pro, but the gateway rejects it with HTTP 400.
    "gemini-3-1-pro": {"off": None, "minimal": None},
    "gemini-2-5-pro": None,
    "gemini-2-5-flash": None,
    "gemini-2-5-flash-lite": None,
}


def _gemini_model_entry(model_id: str) -> dict:
    limits = gemini_model_token_limits(model_id)
    entry: dict = {
        "id": model_id,
        "contextWindow": limits["context"],
        "maxTokens": limits["output"],
    }
    name = canonical_model_name(model_id)
    if name in _GEMINI_THINKING_LEVEL_MAPS:
        entry.update(reasoning=True, input=["text", "image"], inputLimits=_IMAGE_INPUT_LIMITS)
        level_map = _GEMINI_THINKING_LEVEL_MAPS[name]
        if level_map is not None:
            entry["thinkingLevelMap"] = dict(level_map)
    return entry


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
    ``build_pi_api_key`` for the `!command` form every provider gets.

    Models Pi can't serve (Claude older than 4.5, gpt-oss) are dropped from every list first, so
    neither a provider nor the model selector names them."""
    claude_models = {f: m for f, m in claude_models.items() if _serves_claude_model(m)}
    codex_models = [m for m in codex_models if _serves_codex_model(m)]
    providers: dict = {}
    keys: list[list[str]] = [["model"]]
    # Pi expands header values that match an env var name. Our UA contains
    # `/` and a space so it can never collide — safe to pass as a literal.
    ua_headers = {"User-Agent": ucode_user_agent(ug_version(), f"pi/{agent_version('pi')}")}
    request_tags = request_tags_header_value()
    if request_tags:
        # Similarly to UA, request_tags is a JSON object (validated in `request_tags_header_value()`)
        # so it can never collide with an env var name
        ua_headers[AI_GATEWAY_REQUEST_TAGS_HEADER] = request_tags
    gateway_url = next(iter(pi_base_urls.values()), None)
    if gateway_url:
        ua_headers.update(workspace_headers(gateway_url))

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
            "models": [_claude_model_entry(m) for m in claude_ids],
        }
        keys.append(["providers", "databricks-claude"])
    if codex_models:
        providers["databricks-openai"] = {
            "baseUrl": pi_base_urls["openai"],
            "api": "openai-responses",
            "apiKey": api_key,
            "authHeader": True,
            "headers": ua_headers,
            "models": [_gpt_model_entry(m) for m in codex_models],
        }
        keys.append(["providers", "databricks-openai"])
    if gemini_models:
        providers["databricks-gemini"] = {
            "baseUrl": pi_base_urls["gemini"],
            "api": "google-generative-ai",
            "apiKey": api_key,
            "authHeader": True,
            "headers": ua_headers,
            "models": [_gemini_model_entry(m) for m in gemini_models],
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
    `auth-token` serves the CLI's cached token until it nears expiry.

    Always POSIX-quoted: pi runs `!command` values through bash on every OS
    (Git Bash on Windows), which strips the backslashes from a cmd.exe-style
    ``C:\\...\\ug.exe`` path."""
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
    backup_existing_file(PI_CONFIG_PATH, PI_BACKUP_PATH)
    if token is None:
        token = get_databricks_token(state["workspace"], state.get("profile"))
    pi_base_urls = state.get("base_urls", {}).get("pi") or build_pi_base_urls(state["workspace"])
    managed_families = _managed_model_families(state)
    claude_models, codex_models, gemini_models = managed_families or (
        state.get("claude_models") or {},
        state.get("codex_models") or [],
        state.get("gemini_models") or [],
    )
    overlay, managed_keys = render_overlay(
        model,
        build_pi_api_key(state),
        pi_base_urls,
        claude_models,
        codex_models,
        gemini_models,
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


def _managed_model_families(state: dict) -> tuple[dict[str, str], list[str], list[str]] | None:
    """Split a managed config's ``pi_models`` into the per-family inputs Pi's providers need.

    Pi builds one provider block per family, so a flat list has to be classified back out. Returns
    None when the managed models yield no family Pi can serve, leaving the workspace-wide discovery
    lists in play rather than writing a config with no usable provider.
    """
    managed = state.get("pi_models")
    if not isinstance(managed, list) or not managed:
        return None
    claude: dict[str, str] = {}
    codex: list[str] = []
    gemini: list[str] = []
    for model in managed:
        if not isinstance(model, str) or not model.strip():
            continue
        family = classify_model_family(model)
        if family in ANTHROPIC_FAMILIES:
            if _serves_claude_model(model):
                claude.setdefault(family, model)
        elif family == "codex":
            if _serves_codex_model(model):
                codex.append(model)
        elif family == "gemini":
            gemini.append(model)
    if not (claude or codex or gemini):
        return None
    return claude, codex, gemini


def _serves_model(model: str) -> bool:
    family = classify_model_family(model)
    if family in ANTHROPIC_FAMILIES:
        return _serves_claude_model(model)
    if family == "codex":
        return _serves_codex_model(model)
    return family == "gemini"


def default_model(state: dict) -> str | None:
    """Prefer Claude opus → sonnet → haiku; fall back to codex, gemini.

    A managed config's ``pi_default_model`` and ``pi_models`` both win outright: the former is
    the admin's chosen session start, the latter their allowlist (its first model Pi serves).
    Workspace-wide discovery falls back.
    """
    if isinstance(state.get("pi_default_model"), str):
        return state.get("pi_default_model")
    managed = state.get("pi_models")
    if isinstance(managed, list) and managed:
        return next(
            (m for m in managed if isinstance(m, str) and _serves_model(m)),
            managed[0],
        )
    claude_models = state.get("claude_models") or {}
    for family in ("opus", "sonnet", "haiku"):
        if claude_models.get(family) and _serves_claude_model(claude_models[family]):
            return claude_models[family]
    codex_models = [m for m in state.get("codex_models") or [] if _serves_codex_model(m)]
    if codex_models:
        return codex_models[0]
    gemini_models = state.get("gemini_models") or []
    if gemini_models:
        return gemini_models[0]
    return next((m for m in claude_models.values() if _serves_claude_model(m)), None)


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


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    """Launch Pi; it re-resolves its apiKey command per request, so no refresher."""
    token = _configure_launch(state)
    env = build_runtime_env(token)

    proc = subprocess_cross_os.popen([SPEC["binary"], *tool_args], env=env)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()

    raise SystemExit(returncode)


def validate_cmd(binary: str) -> list[str]:
    return [binary, "--print", "say hi in 5 words or less"]
