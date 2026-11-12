#!/bin/bash
#
# Linux twin of scripts/mdm/macos-provision.sh for the MDM E2E test: same
# steps, install layout and arguments, with python3 in place of plutil and
# the Linux builds of Claude Code and Codex. Run as root.
#
#   $4  Databricks workspace URL
#   $5  Service principal OAuth client id
#   $6  Service principal OAuth client secret
#
# Set UG_INSTALL_SPEC to a uv tool spec to install ug from a local checkout.

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

export UV_TOOL_DIR="$PREFIX/tools"
export UV_TOOL_BIN_DIR=/usr/local/bin
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_PYTHON_PREFERENCE=only-managed
mkdir -p /usr/local/bin

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
