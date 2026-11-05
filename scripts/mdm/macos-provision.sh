#!/bin/bash
#
# macOS MDM provisioning script, written for Jamf Pro policies: install
# ug, the Databricks CLI, Claude Code and Codex machine-wide and run
# `ug configure` as root, which installs the workspace policy at
# /etc/unitygateway/managed-settings.json.
# Users then launch with no sudo:
#
#   UG_SKIP_OS_SETTINGS=1 ug claude    (or: ug codex)
#
# Jamf parameters ($1-$3 are reserved by Jamf):
#   $4  Databricks workspace URL, e.g. https://myws.cloud.databricks.com
#   $5  Service principal OAuth client id
#   $6  Service principal OAuth client secret
#
# The token is used for this run only; developers sign in with their own
# OAuth. Needs access to PyPI, GitHub, astral.sh and downloads.claude.ai.

set -euo pipefail
umask 022
export HOME=/var/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
WORKSPACE="${4%/}"
PREFIX=/usr/local/unity-gateway
CLAUDE_RELEASES=https://downloads.claude.ai/claude-code-releases
CODEX_RELEASES=https://github.com/openai/codex/releases/latest/download
UG_REPO=https://github.com/databricks/unity-gateway
UV_URL=https://astral.sh/uv/install.sh
CLI_URL=https://raw.githubusercontent.com
CLI_URL="$CLI_URL/databricks/setup-cli/main/install.sh"
CLT_MARKER=/tmp/.com.apple.dt.CommandLineTools
CLT_MARKER="$CLT_MARKER.installondemand.in-progress"

export UV_TOOL_DIR="$PREFIX/tools"
export UV_TOOL_BIN_DIR=/usr/local/bin
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_PYTHON_PREFERENCE=only-managed
mkdir -p /usr/local/bin

# Get ug from git
if ! xcode-select -p >/dev/null 2>&1; then
  touch "$CLT_MARKER"
  clt="$(softwareupdate -l 2>/dev/null \
    | sed -n 's/^\* Label: \(Command Line Tools.*\)$/\1/p' \
    | tail -n1)"
  softwareupdate -i "$clt"
  rm -f "$CLT_MARKER"
fi

if ! command -v uv >/dev/null; then
  curl -LsSf "$UV_URL" \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
uv tool install --force "git+$UG_REPO"

if [ ! -x /usr/local/bin/databricks ]; then
  curl -fsSL "$CLI_URL" | sh
fi

# Install Claude Code
if [ ! -x /usr/local/bin/claude ]; then
  case "$(uname -m)" in
    arm64) platform=darwin-arm64 ;;
    *) platform=darwin-x64 ;;
  esac
  version="$(curl -fsSL "$CLAUDE_RELEASES/latest")"
  base="$CLAUDE_RELEASES/$version"
  checksum="$(curl -fsSL "$base/manifest.json" \
    | plutil -extract "platforms.$platform.checksum" raw -o - -)"
  curl -fsSL -o /tmp/claude "$base/$platform/claude"
  echo "$checksum  /tmp/claude" | shasum -a 256 -c -
  install -m 755 /tmp/claude /usr/local/bin/claude
  rm -f /tmp/claude
fi

# Install Codex
if [ ! -x /usr/local/bin/codex ]; then
  case "$(uname -m)" in
    arm64) target=aarch64-apple-darwin ;;
    *) target=x86_64-apple-darwin ;;
  esac
  curl -fsSL "$CODEX_RELEASES/codex-$target.tar.gz" \
    | tar -xzf - -C /tmp "codex-$target"
  install -m 755 "/tmp/codex-$target" /usr/local/bin/codex
  rm -f "/tmp/codex-$target"
fi

# Get short lived credential; curl reads the client id and secret from
# stdin (-K -) so they don't appear in its arguments.
resp="$(printf 'user = "%s:%s"\n' "$5" "$6" \
  | curl -sS -K - \
      -d grant_type=client_credentials \
      -d scope=all-apis \
      "$WORKSPACE/oidc/v1/token")"
if ! DATABRICKS_BEARER="$(printf '%s' "$resp" \
    | plutil -extract access_token raw -o - -)"; then
  echo "Token request to $WORKSPACE failed: $resp" >&2
  exit 1
fi
export DATABRICKS_BEARER

ug configure --workspace "$WORKSPACE" --agents claude,codex </dev/null
