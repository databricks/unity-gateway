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
#   3. Writes a PAT-based Databricks CLI profile.
#   4. Runs `ug configure --use-pat` headlessly (this also installs the
#      Databricks CLI and the enabled agent CLIs via npm).
#   5. Probes every agent in the workspace's managed `enabled_agents` with a
#      real one-shot inference call through the AI Gateway.
#
# All inputs are environment variables. JAMF reserves the positional parameters
# $1-$4 (mount point, computer name, user name, and its first script parameter),
# so this script never reads positional parameters.
#
# Required:
#   UG_WORKSPACE_HOST   Databricks workspace URL, e.g. https://myws.cloud.databricks.com
#   UG_PAT              Databricks personal access token for that workspace
#
# Optional:
#   UG_PROFILE_NAME     Databricks CLI profile name to write (default: ug-mdm)
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
#     -e UG_PAT="dapi..." \
#     my-image /path/to/mdm-bootstrap.sh
#
# JAMF usage: JAMF passes positional parameters ($4-$11) rather than env vars,
# and reserves $1-$3 (mount, computer, user). Deploy this script unchanged and
# upload a tiny wrapper as the JAMF policy script, mapping two JAMF parameters
# to the env vars this script reads (using $5/$6 to stay clear of $1-$4):
#
#   #!/bin/bash
#   # JAMF policy parameters: 5 = workspace URL, 6 = PAT
#   export UG_WORKSPACE_HOST="$5"
#   export UG_PAT="$6"
#   exec /usr/local/bin/mdm-bootstrap.sh
#
# Note: a PAT passed as a JAMF parameter is visible in the JAMF policy config and
# logs. For a real fleet, prefer a Databricks service principal (OAuth M2M) as the
# machine identity rather than a shared user PAT (see the team writeup).
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

UG_PROFILE_NAME="${UG_PROFILE_NAME:-ug-mdm}"
UG_INSTALL_SPEC="${UG_INSTALL_SPEC:-git+https://github.com/databricks/unity-gateway}"
UG_NODE_VERSION="${UG_NODE_VERSION:-22.14.0}" # current LTS; overridable
UG_AGENTS="${UG_AGENTS:-}"
UG_SKIP_PROBE="${UG_SKIP_PROBE:-}"

PROBE_PROMPT="say hi in 5 words or less"
NODE_PREFIX="${UG_NODE_PREFIX:-/opt/ug-node}"

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
  [ -n "${UG_PAT:-}" ] || die "UG_PAT is required (a Databricks personal access token)."
  case "$UG_WORKSPACE_HOST" in
    https://*) : ;;
    *) die "UG_WORKSPACE_HOST must start with https:// (got: $UG_WORKSPACE_HOST)." ;;
  esac
  ok "workspace: $UG_WORKSPACE_HOST"
  ok "profile:   $UG_PROFILE_NAME"
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

# ── phase 3: configure headlessly ────────────────────────────────────────────

write_databricks_profile() {
  section "Writing Databricks CLI profile [$UG_PROFILE_NAME]"
  local cfg="${DATABRICKS_CONFIG_FILE:-$HOME/.databrickscfg}"
  local tmp
  tmp="$(mktemp)"
  # Drop any pre-existing block for this profile, keeping every other profile
  # intact, then append a fresh PAT block.
  if [ -f "$cfg" ]; then
    awk -v prof="[$UG_PROFILE_NAME]" '
      $0 == prof { skip = 1; next }
      /^\[/      { skip = 0 }
      !skip      { print }
    ' "$cfg" > "$tmp"
  fi
  {
    printf '[%s]\n' "$UG_PROFILE_NAME"
    printf 'host = %s\n' "$UG_WORKSPACE_HOST"
    printf 'token = %s\n' "$UG_PAT"
    printf 'auth_type = pat\n'
  } >> "$tmp"
  mkdir -p "$(dirname "$cfg")"
  mv "$tmp" "$cfg"
  chmod 600 "$cfg"
  ok "wrote profile to $cfg (mode 600)"
}

configure_ug() {
  section "Configuring ug (headless, PAT)"
  local args=(configure --profile "$UG_PROFILE_NAME" --use-pat)
  [ -n "$UG_AGENTS" ] && args+=(--agents "$UG_AGENTS")
  info "ug ${args[*]}"
  # stdin from /dev/null keeps ug non-interactive: it writes only local settings
  # and skips the sudo OS-managed reconciliation (which would prompt). The
  # OS-managed enforcement is deployed via MDM profiles — see scripts/mdm/.
  ug "${args[@]}" </dev/null
  ok "ug configured"
}

# ── phase 4: verify with a real inference probe ──────────────────────────────

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
  write_databricks_profile
  configure_ug
  probe_enabled_agents
  section "Done"
  ok "ug is installed and configured; enabled agents verified."
  info "Run 'ug' to launch the default agent."
}

main "$@"
