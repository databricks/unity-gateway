#!/bin/bash
#
# Linux MDM provisioning script, the counterpart of macos-provision.sh for
# MDM or configuration management tools: install ug, the Databricks CLI,
# Claude Code and Codex machine-wide and run `ug configure` as root, which
# writes the workspace policy at /etc/unitygateway/managed-settings.json
# and the Claude Code and Codex managed settings. It also adds `claude` and
# `codex` aliases to the system zsh and bash startup files, so users launch
# with `claude` or `codex` in a new terminal, with no sudo.
#
# Arguments match macos-provision.sh, so $1-$3 are ignored:
#   $4  Databricks workspace URL, starting with https://
#   $5  Service principal OAuth client id
#   $6  Service principal OAuth client secret
#
# Set UG_INSTALL_SPEC to a uv tool spec to install ug from somewhere other
# than the repository's main branch, e.g. a local checkout.
#
# The token is used for this run only; developers sign in with their own
# OAuth. Needs access to PyPI, GitHub, astral.sh and downloads.claude.ai.

set -euo pipefail
umask 022
export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
WORKSPACE="${4%/}"
PREFIX=/usr/local/unity-gateway
CLAUDE_RELEASES=https://downloads.claude.ai/claude-code-releases
CODEX_RELEASES=https://github.com/openai/codex/releases/latest/download
UG_REPO=https://github.com/databricks/unity-gateway
UG_INSTALL_SPEC="${UG_INSTALL_SPEC:-git+$UG_REPO}"
UV_URL=https://astral.sh/uv/install.sh
CLI_URL=https://raw.githubusercontent.com
CLI_URL="$CLI_URL/databricks/setup-cli/main/install.sh"

# ug must be in a shared path: the machine-wide agent settings
# run it by absolute path for every user.
export UV_TOOL_DIR="$PREFIX/tools"
export UV_TOOL_BIN_DIR=/usr/local/bin
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_PYTHON_PREFERENCE=only-managed
mkdir -p /usr/local/bin

# Get ug from git; python3 reads the JSON responses below
missing=()
for cmd in curl git python3 tar unzip; do
  command -v "$cmd" >/dev/null || missing+=("$cmd")
done
if [ "${#missing[@]}" -gt 0 ]; then
  if command -v apt-get >/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  elif command -v dnf >/dev/null; then
    dnf install -y -q "${missing[@]}"
  else
    echo "Install ${missing[*]} and run this again." >&2
    exit 1
  fi
fi

if ! command -v uv >/dev/null; then
  curl -LsSf "$UV_URL" \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
uv tool install --force "$UG_INSTALL_SPEC"

if [ ! -x /usr/local/bin/databricks ]; then
  curl -fsSL "$CLI_URL" | sh
fi

# Download into a private temporary directory, not fixed /tmp paths
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Install Claude Code
if [ ! -x /usr/local/bin/claude ]; then
  case "$(uname -m)" in
    aarch64 | arm64) platform=linux-arm64 ;;
    *) platform=linux-x64 ;;
  esac
  version="$(curl -fsSL "$CLAUDE_RELEASES/latest")"
  base="$CLAUDE_RELEASES/$version"
  checksum="$(curl -fsSL "$base/manifest.json" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["platforms"][sys.argv[1]]["checksum"])' "$platform")"
  curl -fsSL -o "$tmp/claude" "$base/$platform/claude"
  echo "$checksum  $tmp/claude" | sha256sum -c -
  install -m 755 "$tmp/claude" /usr/local/bin/claude
fi

# Install Codex from its release package, which also includes the
# codex-code-mode-host helper Code Mode needs next to codex.
if [ ! -x "$PREFIX/codex/bin/codex" ]; then
  case "$(uname -m)" in
    aarch64 | arm64) target=aarch64-unknown-linux-musl ;;
    *) target=x86_64-unknown-linux-musl ;;
  esac
  package="codex-package-$target.tar.gz"
  curl -fsSL -o "$tmp/$package" "$CODEX_RELEASES/$package"
  curl -fsSL "$CODEX_RELEASES/codex-package_SHA256SUMS" \
    | grep "  $package\$" | (cd "$tmp" && sha256sum -c -)
  mkdir -p "$PREFIX/codex"
  tar -xzf "$tmp/$package" -C "$PREFIX/codex"
  ln -sfn "$PREFIX/codex/bin/codex" /usr/local/bin/codex
  ln -sfn "$PREFIX/codex/bin/codex-code-mode-host" \
    /usr/local/bin/codex-code-mode-host
fi

# Get short lived credential; curl reads the client id and secret from
# stdin (-K -) so they don't appear in its arguments.
resp="$(printf 'user = "%s:%s"\n' "$5" "$6" \
  | curl -sS -K - \
      -d grant_type=client_credentials \
      --data-urlencode 'scope=ai-gateway:read unity-catalog:read' \
      "$WORKSPACE/oidc/v1/token")"
if ! DATABRICKS_BEARER="$(printf '%s' "$resp" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')"; then
  echo "Token request to $WORKSPACE failed: $resp" >&2
  exit 1
fi
export DATABRICKS_BEARER

ug configure --global --workspace "$WORKSPACE" \
  --agents claude,codex </dev/null

# Aliases for every user's interactive zsh and bash shells, in the
# Debian and Red Hat locations. The block is rewritten on each run.
for rc in /etc/bash.bashrc /etc/bashrc /etc/zsh/zshrc /etc/zshrc; do
  [ -f "$rc" ] || continue
  sed -i '/^# BEGIN unity-gateway/,/^# END unity-gateway/d' "$rc"
  cat >>"$rc" <<'ALIASES'
# BEGIN unity-gateway (managed by MDM)
alias claude='UG_SKIP_OS_SETTINGS=1 ug claude'
alias codex='UG_SKIP_OS_SETTINGS=1 ug codex'
# END unity-gateway
ALIASES
done
