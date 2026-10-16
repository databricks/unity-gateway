#!/usr/bin/env bash
#
# Unity Gateway (ug) MDM / JAMF bootstrap.
#
# Provisions a fresh macOS (or Linux) machine end to end so that, when this
# script finishes, `ug` and every workspace-enabled coding agent work headlessly
# with no browser login. Intended to be uploaded into JAMF and run as root on a
# bare machine, and to be testable inside a fresh container.
#
# The script:
#   1. Ensures ug's external prerequisites exist (curl/git, uv, node/npm).
#   2. Installs ug via uv.
#   3. Mints a short-lived service-principal (OAuth M2M) token from
#      UG_CLIENT_ID/UG_CLIENT_SECRET and exposes it to ug via
#      DATABRICKS_BEARER_COMMAND, so the token is re-minted on demand and never
#      written to disk.
#   4. Runs `ug configure --workspace <url>` headlessly (this also installs the
#      Databricks CLI and the enabled agent CLIs via npm).
#   5. Probes every agent in the workspace's managed `enabled_agents` with a
#      real one-shot inference call through the AI Gateway.
#
# Provisioning vs. runtime auth: the SP token above is a scoped, ephemeral
# PROVISIONING credential. It is never persisted (`ug configure` writes only the
# workspace and model lists, not a token, and never sets use_pat), so once this
# script exits a real developer's `ug` launch falls back to their own
# per-developer OAuth for inference. The bootstrap never becomes the fleet's
# standing inference identity.
#
# All inputs are environment variables. JAMF reserves the positional parameters
# $1-$4 (mount point, computer name, user name, and its first script parameter),
# so this script never reads positional parameters.
#
# Required:
#   UG_WORKSPACE_HOST   Databricks workspace URL, e.g. https://myws.cloud.databricks.com
#   UG_CLIENT_ID        Service-principal OAuth client id (application id)
#   UG_CLIENT_SECRET    Service-principal OAuth client secret
#
# Optional:
#   UG_AGENTS           Comma-separated agents to force (e.g. "claude,codex").
#                       Default: let the workspace's managed enabled_agents decide.
#   UG_INSTALL_SPEC     uv install spec for ug
#                       (default: git+https://github.com/databricks/unity-gateway)
#   UG_NODE_VERSION     Node.js version to install if node is absent (default below)
#   UG_SKIP_PROBE       If set to a non-empty value, skip the inference probe.
#
# ─────────────────────────────────────────────────────────────────────────────
# Container / CI usage (the secret is injected at run time, never stored here):
#
#   docker run --rm \
#     -e UG_WORKSPACE_HOST="https://myws.cloud.databricks.com" \
#     -e UG_CLIENT_ID="<sp-application-id>" \
#     -e UG_CLIENT_SECRET="<sp-secret>" \
#     my-image /path/to/mdm-bootstrap.sh
#
# JAMF usage: JAMF passes positional parameters ($4-$11) rather than env vars,
# and reserves $1-$3 (mount, computer, user). Deploy this script unchanged and
# upload a tiny wrapper as the JAMF policy script, mapping three JAMF parameters
# to the env vars this script reads (using $5-$7 to stay clear of $1-$4):
#
#   #!/bin/bash
#   # JAMF policy parameters: 5 = workspace URL, 6 = SP client id, 7 = SP secret
#   export UG_WORKSPACE_HOST="$5"
#   export UG_CLIENT_ID="$6"
#   export UG_CLIENT_SECRET="$7"
#   exec /usr/local/bin/mdm-bootstrap.sh
#
# Prefer a Databricks service principal (OAuth M2M) as the machine identity over
# a shared user PAT: the token minted here is short-lived and scoped, and this
# script never writes any credential to disk (contrast a PAT profile in
# ~/.databrickscfg, which would persist a replayable inference credential).
#
# OS-managed enforcement layer:
# This script provisions ug + LOCAL settings and runs `ug configure` NON-
# interactively, so it never writes the OS-managed files
# (/Library/Application Support/ClaudeCode/managed-settings.json,
# /etc/codex/managed_config.toml) and never prompts for a sudo password.
# `ug claude` / `ug codex` work off the local settings regardless. Deploy the
# OS-managed enforcement separately as JAMF configuration profiles
# (com.anthropic.claudecode, com.openai.codex) — see scripts/mdm/README.md — so
# gateway routing is enforced even for bare `claude` / `codex` launches.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── configuration ────────────────────────────────────────────────────────────

UG_INSTALL_SPEC="${UG_INSTALL_SPEC:-git+https://github.com/databricks/unity-gateway}"
UG_NODE_VERSION="${UG_NODE_VERSION:-22.14.0}" # current LTS; overridable
UG_AGENTS="${UG_AGENTS:-}"
UG_SKIP_PROBE="${UG_SKIP_PROBE:-}"

PROBE_PROMPT="say hi in 5 words or less"
NODE_PREFIX="${UG_NODE_PREFIX:-/opt/ug-node}"

# Path to the ephemeral bearer broker written by setup_bearer_broker; cleaned up
# on exit. Empty until then.
BEARER_BROKER=""

# ── UI helpers ───────────────────────────────────────────────────────────────

if [ -t 1 ]; then
  _c_red=$'\033[31m'; _c_grn=$'\033[32m'; _c_ylw=$'\033[33m'
  _c_blu=$'\033[34m'; _c_bld=$'\033[1m'; _c_rst=$'\033[0m'
else
  _c_red=; _c_grn=; _c_ylw=; _c_blu=; _c_bld=; _c_rst=
fi

section() { printf '\n%s==> %s%s\n' "$_c_blu$_c_bld" "$*" "$_c_rst"; }
info()    { printf '    %s\n' "$*"; }
ok()      { printf '  %s✓%s %s\n' "$_c_grn" "$_c_rst" "$*"; }
warn()    { printf '  %s!%s %s\n' "$_c_ylw" "$_c_rst" "$*" >&2; }
die()     { printf '  %s✗ %s%s\n' "$_c_red$_c_bld" "$*" "$_c_rst" >&2; exit 1; }

# ── platform detection ───────────────────────────────────────────────────────

OS="$(uname -s)"
ARCH="$(uname -m)"

is_macos() { [ "$OS" = "Darwin" ]; }
is_linux() { [ "$OS" = "Linux" ]; }

# node's release naming for the current platform/arch.
node_platform() {
  case "$OS" in
    Darwin) printf 'darwin' ;;
    Linux)  printf 'linux' ;;
    *)      die "Unsupported OS for automatic node install: $OS" ;;
  esac
}
node_arch() {
  case "$ARCH" in
    x86_64|amd64) printf 'x64' ;;
    arm64|aarch64) printf 'arm64' ;;
    *) die "Unsupported CPU architecture for automatic node install: $ARCH" ;;
  esac
}

# Root check: JAMF runs as root. Some installs (apt, /opt, /etc) need it. We do
# not hard-require root so the script is also runnable in a rootless container,
# but we warn when a step that wants root is reached without it.
IS_ROOT=0
[ "$(id -u)" = "0" ] && IS_ROOT=1

as_root() {
  if [ "$IS_ROOT" = "1" ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    die "This step needs root but neither root nor sudo is available: $*"
  fi
}

# Prepend a directory to PATH once, for this process and for child ug/agent runs.
add_to_path() {
  case ":$PATH:" in
    *":$1:"*) : ;;
    *) PATH="$1:$PATH"; export PATH ;;
  esac
}

# ── input validation ─────────────────────────────────────────────────────────

require_inputs() {
  section "Validating inputs"
  [ -n "${UG_WORKSPACE_HOST:-}" ] || die "UG_WORKSPACE_HOST is required (e.g. https://myws.cloud.databricks.com)."
  [ -n "${UG_CLIENT_ID:-}" ] || die "UG_CLIENT_ID is required (the service principal's OAuth client id)."
  [ -n "${UG_CLIENT_SECRET:-}" ] || die "UG_CLIENT_SECRET is required (the service principal's OAuth client secret)."
  case "$UG_WORKSPACE_HOST" in
    https://*) : ;;
    *) die "UG_WORKSPACE_HOST must start with https:// (got: $UG_WORKSPACE_HOST)." ;;
  esac
  ok "workspace: $UG_WORKSPACE_HOST"
  ok "auth:      service principal $UG_CLIENT_ID (OAuth M2M, ephemeral)"
  if [ -n "$UG_AGENTS" ]; then
    ok "agents override: $UG_AGENTS"
  else
    info "agents: from workspace enabled_agents"
  fi
}

# ── phase 1: dependencies ────────────────────────────────────────────────────

ensure_apt_packages() {
  # Only meaningful on Debian/Ubuntu-family Linux (the fresh-container case).
  command -v apt-get >/dev/null 2>&1 || return 1
  as_root apt-get update -qq
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
}

ensure_curl_and_git() {
  section "Dependency: curl + git"
  local missing=()
  command -v curl >/dev/null 2>&1 || missing+=("curl")
  command -v git  >/dev/null 2>&1 || missing+=("git")
  if [ "${#missing[@]}" -eq 0 ]; then
    ok "curl and git present"
    return
  fi
  info "installing: ${missing[*]}"
  if is_linux && command -v apt-get >/dev/null 2>&1; then
    ensure_apt_packages ca-certificates "${missing[@]}"
  elif is_macos; then
    die "Missing ${missing[*]} on macOS. Install the Xcode Command Line Tools (xcode-select --install)."
  else
    die "Cannot install ${missing[*]} automatically on this platform. Install them and re-run."
  fi
  command -v curl >/dev/null 2>&1 || die "curl still not on PATH after install."
  command -v git  >/dev/null 2>&1 || die "git still not on PATH after install."
  ok "curl and git installed"
}

ensure_uv() {
  section "Dependency: uv"
  add_to_path "$HOME/.local/bin"
  add_to_path "$HOME/.cargo/bin"
  if command -v uv >/dev/null 2>&1; then
    ok "uv present ($(uv --version 2>/dev/null))"
    return
  fi
  info "installing uv via astral.sh install script"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  add_to_path "$HOME/.local/bin"
  add_to_path "$HOME/.cargo/bin"
  command -v uv >/dev/null 2>&1 || die "uv still not on PATH after install. Check ~/.local/bin."
  ok "uv installed ($(uv --version 2>/dev/null))"
}

ensure_node() {
  section "Dependency: node + npm"
  add_to_path "$NODE_PREFIX/bin"
  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    ok "node present ($(node --version 2>/dev/null)), npm present ($(npm --version 2>/dev/null))"
    return
  fi
  local plat arch tarball url dest
  plat="$(node_platform)"
  arch="$(node_arch)"
  tarball="node-v${UG_NODE_VERSION}-${plat}-${arch}.tar.gz"
  url="https://nodejs.org/dist/v${UG_NODE_VERSION}/${tarball}"
  info "installing Node.js v${UG_NODE_VERSION} for ${plat}-${arch} into ${NODE_PREFIX}"
  as_root mkdir -p "$NODE_PREFIX"
  dest="$(mktemp -d)"
  curl -fsSL "$url" -o "$dest/$tarball" || die "Failed to download Node.js from $url"
  # Strip the top-level node-vX-plat-arch/ directory so binaries land in $NODE_PREFIX/bin.
  as_root tar -xzf "$dest/$tarball" -C "$NODE_PREFIX" --strip-components=1
  rm -rf "$dest"
  add_to_path "$NODE_PREFIX/bin"
  command -v node >/dev/null 2>&1 || die "node still not on PATH after install ($NODE_PREFIX/bin)."
  command -v npm  >/dev/null 2>&1 || die "npm still not on PATH after install ($NODE_PREFIX/bin)."
  ok "node installed ($(node --version)), npm ($(npm --version))"
}

# ── phase 2: install ug ──────────────────────────────────────────────────────

install_ug() {
  section "Installing Unity Gateway (ug)"
  info "uv tool install --force $UG_INSTALL_SPEC"
  uv tool install --force "$UG_INSTALL_SPEC"
  # uv tool binaries live in the uv tool bin dir; make sure it's reachable.
  add_to_path "$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")"
  add_to_path "$HOME/.local/bin"
  command -v ug >/dev/null 2>&1 || die "ug is not on PATH after install. Check the uv tool bin directory."
  ok "ug installed ($(ug --version 2>/dev/null))"
}

# ── phase 3: ephemeral provisioning credential ───────────────────────────────

cleanup_bearer_broker() {
  [ -n "$BEARER_BROKER" ] && rm -f "$BEARER_BROKER" 2>/dev/null || true
}

# Mint provisioning auth from the service principal's client_id/secret without
# ever writing a credential to disk. ug reads DATABRICKS_BEARER_COMMAND and runs
# it on every token fetch, using the bare stdout as the bearer and failing closed
# if it errors — so the token stays short-lived, is re-minted on demand, and is
# never persisted. `ug configure` then needs no --profile and no --use-pat.
setup_bearer_broker() {
  section "Preparing ephemeral provisioning credential (SP OAuth M2M)"
  # The broker reads these from its environment; export so ug's child invocation
  # (and its own child curl) inherit them. The secret is never baked into the
  # DATABRICKS_BEARER_COMMAND string (which ug echoes in error diagnostics).
  export UG_WORKSPACE_HOST UG_CLIENT_ID UG_CLIENT_SECRET
  BEARER_BROKER="$(mktemp)"
  trap cleanup_bearer_broker EXIT
  chmod 700 "$BEARER_BROKER"
  cat > "$BEARER_BROKER" <<'BROKER'
#!/usr/bin/env bash
# Ephemeral bearer broker: mint a short-lived workspace token from the service
# principal's client_id/secret and print the bare token to stdout. Reads creds
# from the environment only. curl gets the credentials through a config file on
# stdin (-K -) so they never land in argv/ps.
set -euo pipefail
: "${UG_WORKSPACE_HOST:?}" "${UG_CLIENT_ID:?}" "${UG_CLIENT_SECRET:?}"
resp="$(printf 'user = "%s:%s"\n' "$UG_CLIENT_ID" "$UG_CLIENT_SECRET" \
  | curl -sSf --max-time 10 -K - \
      --data-urlencode 'grant_type=client_credentials' \
      --data-urlencode 'scope=all-apis' \
      "${UG_WORKSPACE_HOST%/}/oidc/v1/token")"
printf '%s' "$resp" | node -e '
  let d = "";
  process.stdin.on("data", c => (d += c)).on("end", () => {
    let t = "";
    try { t = JSON.parse(d).access_token || ""; } catch (e) { process.exit(3); }
    if (!t) process.exit(4);
    process.stdout.write(t);
  });
'
BROKER
  # Fail fast with an actionable message if the SP creds cannot mint a token,
  # rather than surfacing later as an opaque 401 during configure.
  if ! "$BEARER_BROKER" >/dev/null; then
    die "Could not mint a token from UG_CLIENT_ID/UG_CLIENT_SECRET against ${UG_WORKSPACE_HOST%/}/oidc/v1/token. Check the service principal's credentials and its access to the workspace."
  fi
  export DATABRICKS_BEARER_COMMAND="$BEARER_BROKER"
  ok "ephemeral SP token verified; ug re-mints on demand (nothing written to disk)"
}

# ── phase 4: configure headlessly ────────────────────────────────────────────

configure_ug() {
  section "Configuring ug (headless, ephemeral SP token)"
  local args=(configure --workspace "$UG_WORKSPACE_HOST")
  [ -n "$UG_AGENTS" ] && args+=(--agents "$UG_AGENTS")
  info "ug ${args[*]}"
  # stdin from /dev/null keeps ug non-interactive: it writes only local settings
  # and skips the sudo OS-managed reconciliation (which would prompt). The
  # OS-managed enforcement is deployed via MDM profiles — see scripts/mdm/.
  # DATABRICKS_BEARER_COMMAND (set above) supplies auth; with no --profile and no
  # --use-pat, ug persists workspace + model lists only and never sets use_pat,
  # so post-provisioning launches fall back to per-developer OAuth.
  ug "${args[@]}" </dev/null
  ok "ug configured"
}

# ── phase 5: verify with a real inference probe ──────────────────────────────

# Map a CODING_AGENT_* proto enum to the ug agent name (see
# src/ucode/managed_config.py:AGENT_ENUM_TO_TOOL).
enum_to_tool() {
  case "$1" in
    CODING_AGENT_CLAUDE_CODE) printf 'claude' ;;
    CODING_AGENT_CODEX)       printf 'codex' ;;
    CODING_AGENT_GEMINI)      printf 'gemini' ;;
    CODING_AGENT_COPILOT)     printf 'copilot' ;;
    CODING_AGENT_PI)          printf 'pi' ;;
    CODING_AGENT_OPENCODE)    printf 'opencode' ;;
    *)                        printf '' ;;
  esac
}

# Run one agent's non-interactive one-shot through ug. Mirrors each agent's
# validate_cmd recipe (src/ucode/agents/<name>.py). Returns 0 on non-empty output.
probe_agent() {
  local tool="$1" out rc
  local run=(ug "$tool")
  case "$tool" in
    claude)   run+=(-p "$PROBE_PROMPT" --max-turns 1) ;;
    codex)    run+=(exec --skip-git-repo-check "$PROBE_PROMPT") ;;
    gemini)   run+=(-p "$PROBE_PROMPT") ;;
    opencode) run+=(run "$PROBE_PROMPT") ;;
    copilot)  run+=(--prompt "$PROBE_PROMPT" --allow-all-tools) ;;
    pi)       run+=(--print "$PROBE_PROMPT") ;;
    *) warn "no probe recipe for '$tool'; skipping"; return 2 ;;
  esac
  # stdin from /dev/null so the launch stays non-interactive too (no per-launch
  # sudo managed-settings prompt); -p/exec read the prompt from argv, not stdin.
  if command -v timeout >/dev/null 2>&1; then
    out="$(timeout 180 "${run[@]}" </dev/null 2>/dev/null)" && rc=0 || rc=$?
  else
    out="$("${run[@]}" </dev/null 2>/dev/null)" && rc=0 || rc=$?
  fi
  if [ "$rc" -eq 0 ] && [ -n "${out//[[:space:]]/}" ]; then
    ok "$tool responded: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-60)"
    return 0
  fi
  warn "$tool probe failed (exit $rc)"
  return 1
}

probe_enabled_agents() {
  section "Probing enabled agents (real inference)"
  # The probe authenticates with the provisioning SP token (via
  # DATABRICKS_BEARER_COMMAND); it is a provisioning sanity check, not the
  # representative runtime path — real developer launches use per-developer OAuth.
  if [ -n "$UG_SKIP_PROBE" ]; then
    info "UG_SKIP_PROBE set — skipping inference probe"
    return 0
  fi
  local export_json enums
  export_json="$(ug export 2>/dev/null)" || die "ug export failed; cannot determine enabled_agents."
  # Parse enabled_agents[].agent with node (guaranteed present after phase 1).
  enums="$(printf '%s' "$export_json" | node -e '
    let d = "";
    process.stdin.on("data", c => d += c).on("end", () => {
      try {
        const j = JSON.parse(d);
        const a = (j.enabled_agents || []).map(x => x && x.agent).filter(Boolean);
        process.stdout.write(a.join("\n"));
      } catch (e) { process.exit(3); }
    });
  ')" || die "Could not parse enabled_agents from ug export output."

  if [ -z "$enums" ]; then
    warn "no enabled_agents in the managed config; nothing to probe"
    return 0
  fi

  local failed=0 probed=0 enum tool
  while IFS= read -r enum; do
    [ -n "$enum" ] || continue
    tool="$(enum_to_tool "$enum")"
    if [ -z "$tool" ]; then
      warn "unknown agent enum '$enum'; skipping"
      continue
    fi
    probed=$((probed + 1))
    probe_agent "$tool" || failed=$((failed + 1))
  done <<EOF
$enums
EOF

  info "probed $probed agent(s), $failed failed"
  [ "$failed" -eq 0 ] || die "$failed enabled agent(s) failed their inference probe."
  ok "all enabled agents responded"
}

# ── main ─────────────────────────────────────────────────────────────────────

main() {
  section "Unity Gateway MDM bootstrap ($OS/$ARCH)"
  require_inputs
  ensure_curl_and_git
  ensure_uv
  ensure_node
  install_ug
  setup_bearer_broker
  configure_ug
  probe_enabled_agents
  section "Done"
  ok "ug is installed and configured; enabled agents verified."
  info "Run 'ug' to launch the default agent."
}

main "$@"
