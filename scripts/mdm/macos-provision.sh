#!/bin/bash
#
# macOS MDM provisioning script, written for Jamf Pro policies: install
# ug, the Databricks CLI, Claude Code and Codex machine-wide and run
# `ug configure` as root, which installs the workspace policy at
# /etc/unitygateway/managed-settings.json. It also adds `claude` and
# `codex` aliases to /etc/zshrc and /etc/bashrc, so users launch with
# `claude` or `codex` in a new terminal, with no sudo.
#
# Jamf parameters ($1-$3 are reserved by Jamf):
#   $4  Databricks workspace URL, starting with https://
#   $5  Service principal OAuth client id
#   $6  Service principal OAuth client secret
#
# The token is used for this run only; developers sign in with their own
# OAuth. Needs access to PyPI, GitHub, astral.sh, nodejs.org and npmjs.org.

set -euo pipefail
umask 022
export HOME=/var/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
WORKSPACE="${4%/}"
PREFIX=/usr/local/unity-gateway
NODE_VERSION=22.23.5
NODE_SHA256=89fadefd7798158d8990ca9ed7dbb58f8ca7593adb92fe3da8a445db99415533
UG_REPO=https://github.com/databricks/unity-gateway
UV_URL=https://astral.sh/uv/install.sh
CLI_URL=https://raw.githubusercontent.com
CLI_URL="$CLI_URL/databricks/setup-cli/main/install.sh"
CLT_MARKER=/tmp/.com.apple.dt.CommandLineTools
CLT_MARKER="$CLT_MARKER.installondemand.in-progress"

# ug must be in a shared path: the machine-wide agent settings
# run it by absolute path for every user.
export UV_TOOL_DIR="$PREFIX/tools"
export UV_TOOL_BIN_DIR=/usr/local/bin
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_PYTHON_PREFERENCE=only-managed
# ug's npm installer must put agents on every user's PATH, not in root's home.
export npm_config_prefix=/usr/local
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

# ug configure installs missing agents with npm; provide a shared Node.js runtime.
if ! command -v node >/dev/null || ! command -v npm >/dev/null; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  package="node-v$NODE_VERSION.pkg"
  /usr/bin/curl -fsSL -o "$tmp/$package" \
    "https://nodejs.org/dist/v$NODE_VERSION/$package"
  printf '%s  %s\n' "$NODE_SHA256" "$tmp/$package" | /usr/bin/shasum -a 256 -c -
  /usr/sbin/installer -pkg "$tmp/$package" -target /
fi

# Get short lived credential; curl reads the client id and secret from
# stdin (-K -) so they don't appear in its arguments.
resp="$(printf 'user = "%s:%s"\n' "$5" "$6" \
  | curl -sS -K - \
      -d grant_type=client_credentials \
      --data-urlencode 'scope=ai-gateway:read unity-catalog:read' \
      "$WORKSPACE/oidc/v1/token")"
if ! DATABRICKS_BEARER="$(printf '%s' "$resp" \
    | plutil -extract access_token raw -o - -)"; then
  echo "Token request to $WORKSPACE failed: $resp" >&2
  exit 1
fi
export DATABRICKS_BEARER

/usr/local/bin/ug configure --global --mdm --workspace "$WORKSPACE" \
  --agents claude,codex </dev/null
unset DATABRICKS_BEARER resp

# Aliases for every user's interactive zsh and bash shells. The block is
# rewritten on each run, which restores it if a macOS update resets
# the file.
for rc in /etc/zshrc /etc/bashrc; do
  [ -f "$rc" ] || continue
  sed -i '' '/^# BEGIN unity-gateway/,/^# END unity-gateway/d' "$rc"
  cat >>"$rc" <<'ALIASES'
# BEGIN unity-gateway (managed by MDM)
alias claude='UG_SKIP_OS_SETTINGS=1 ug claude'
alias codex='UG_SKIP_OS_SETTINGS=1 ug codex'
# END unity-gateway
ALIASES
done
