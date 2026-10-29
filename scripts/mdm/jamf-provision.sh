#!/bin/bash
#
# Jamf policy script: install ug, the Databricks CLI, Claude Code and
# Codex machine-wide and run `ug configure` as root, which installs the
# workspace policy at /etc/unitygateway/managed-settings.json.
# Developers, admin or not, then launch with:
#
#   UG_SKIP_OS_SETTINGS=1 ug claude    (or: ug codex)
#
# Jamf parameter ($1-$3 are reserved by Jamf):
#   $4  Databricks workspace URL, e.g. https://myws.cloud.databricks.com
#
# The service principal's credentials come from a root-only file that
# the credentials package installs. The token is used for this run only;
# developers sign in with their own OAuth. Needs access to PyPI, GitHub,
# astral.sh and downloads.claude.ai.

set -euo pipefail
umask 022
export HOME=/var/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
WORKSPACE="${4%/}"
CREDS="/Library/Application Support/UnityGateway/provision.env"
PREFIX=/usr/local/unity-gateway
CLAUDE_RELEASES=https://downloads.claude.ai/claude-code-releases
CODEX_RELEASES=https://github.com/openai/codex/releases/latest/download
UG_REPO=https://github.com/databricks/unity-gateway
UV_URL=https://astral.sh/uv/install.sh
CLI_URL=https://raw.githubusercontent.com
CLI_URL="$CLI_URL/databricks/setup-cli/main/install.sh"
CLT_MARKER=/tmp/.com.apple.dt.CommandLineTools
CLT_MARKER="$CLT_MARKER.installondemand.in-progress"

# Only trust a credentials file that root owns and nobody else can read.
if [ "$(stat -f '%u %Lp' "$CREDS" 2>/dev/null)" != "0 600" ]; then
  echo "Missing or unprotected $CREDS; install the credentials package" >&2
  exit 1
fi

export UV_TOOL_DIR="$PREFIX/tools"
export UV_TOOL_BIN_DIR=/usr/local/bin
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_PYTHON_PREFERENCE=only-managed
mkdir -p /usr/local/bin

# uv installs ug from git, which comes with the Command Line Tools.
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

# Claude Code's installer writes to root's home, so fetch the
# release binary directly.
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

# Codex's npm package needs Node, so install its release binary instead.
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

# The credentials stay out of every command line: sed sees only the
# file path, printf is a shell builtin, and curl reads them from stdin.
client_id="$(sed -n 's/^UG_CLIENT_ID=//p' "$CREDS")"
client_secret="$(sed -n 's/^UG_CLIENT_SECRET=//p' "$CREDS")"
resp="$(printf 'user = "%s:%s"\n' "$client_id" "$client_secret" \
  | curl -sS -K - \
      -d grant_type=client_credentials -d scope=all-apis \
      "$WORKSPACE/oidc/v1/token")"
unset client_secret
if ! DATABRICKS_BEARER="$(printf '%s' "$resp" \
    | plutil -extract access_token raw -o - -)"; then
  echo "Token request to $WORKSPACE failed: $resp" >&2
  exit 1
fi
export DATABRICKS_BEARER

ug configure --workspace "$WORKSPACE" --agents claude,codex </dev/null
