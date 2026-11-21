#!/bin/bash
#
# Linux MDM provisioning script, the counterpart of macos-provision.sh for
# MDM or configuration management tools: install ug, the Databricks CLI
# and Node.js machine-wide and run `ug configure --mdm` as root, which
# installs Claude Code and Codex with npm and writes the workspace policy
# at /etc/unitygateway/managed-settings.json and their managed settings.
# It also adds `claude` and `codex` aliases to the system zsh and bash
# startup files, so users launch with `claude` or `codex` in a new
# terminal, with no sudo.
#
# Arguments match macos-provision.sh, so $1-$3 are ignored:
#   $4  Databricks workspace URL, starting with https://
#   $5  Service principal OAuth client id
#   $6  Service principal OAuth client secret
#
# The token is used for this run only; developers sign in with their own
# OAuth. Needs access to PyPI, GitHub, astral.sh, nodejs.org and npmjs.org.

set -euo pipefail
umask 022
if [ "$(id -u)" -ne 0 ] || [ "$#" -lt 6 ]; then
  echo "Usage: run as root with arguments _ _ _ <workspace-url> <client-id> <client-secret>" >&2
  exit 2
fi
case "$4" in
  https://?*) ;;
  *) echo "The workspace URL must start with https://" >&2; exit 2 ;;
esac
export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
WORKSPACE="${4%/}"
PREFIX=/usr/local/unity-gateway
NODE_VERSION=22.23.5
NODE_SHA256_X64=c54118104acc524633b5abe106bbf44a53126ec8b4e5fa105cb92b5d7de8a1a1
NODE_SHA256_ARM64=8bd3dc69eee8f3f226315a80d7d1d4faccf217a4c8fae500168ac2e38f3bdba3
UG_REPO=https://github.com/databricks/unity-gateway
UV_URL=https://astral.sh/uv/install.sh
CLI_URL=https://raw.githubusercontent.com
CLI_URL="$CLI_URL/databricks/setup-cli/main/install.sh"

# ug must be in a shared path: the machine-wide agent settings
# run it by absolute path for every user.
export UV_TOOL_DIR="$PREFIX/tools"
export UV_TOOL_BIN_DIR=/usr/local/bin
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_PYTHON_PREFERENCE=only-managed
# ug's npm installer must put agents on every user's PATH, not in root's home.
export npm_config_prefix=/usr/local
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
uv tool install --force "git+$UG_REPO"

if [ ! -x /usr/local/bin/databricks ]; then
  curl -fsSL "$CLI_URL" | sh
fi

# ug configure installs missing agents with npm; provide a shared Node.js runtime.
# Claude Code needs Node.js 22 or newer.
node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$node_major" -lt 22 ] || ! command -v npm >/dev/null; then
  case "$(uname -m)" in
    aarch64 | arm64) arch=arm64 sha="$NODE_SHA256_ARM64" ;;
    x86_64 | amd64) arch=x64 sha="$NODE_SHA256_X64" ;;
    *) echo "Unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;;
  esac
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  package="node-v$NODE_VERSION-linux-$arch.tar.gz"
  curl -fsSL -o "$tmp/$package" \
    "https://nodejs.org/dist/v$NODE_VERSION/$package"
  printf '%s  %s\n' "$sha" "$tmp/$package" | sha256sum -c -
  mkdir -p "$PREFIX/node"
  tar -xzf "$tmp/$package" -C "$PREFIX/node" --strip-components=1 --no-same-owner
  for bin in node npm npx; do
    ln -sfn "$PREFIX/node/bin/$bin" "/usr/local/bin/$bin"
  done
fi

# Get short lived credential; curl reads the client id and secret from
# stdin (-K -) so they don't appear in its arguments.
resp="$(printf 'user = "%s:%s"\n' "$5" "$6" \
  | curl -sS --proto '=https' --max-time 60 -K - \
      -d grant_type=client_credentials \
      --data-urlencode 'scope=ai-gateway:read unity-catalog:read' \
      "$WORKSPACE/oidc/v1/token")"
if ! DATABRICKS_BEARER="$(printf '%s' "$resp" \
    | python3 -I -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')"; then
  echo "Token request to $WORKSPACE failed: $resp" >&2
  exit 1
fi
export DATABRICKS_BEARER

/usr/local/bin/ug configure --global --mdm --workspace "$WORKSPACE" \
  --agents claude,codex </dev/null
unset DATABRICKS_BEARER resp

# Aliases for every user's interactive zsh and bash shells, in the
# Debian and Red Hat locations. The block is rewritten on each run.
for rc in /etc/bash.bashrc /etc/bashrc /etc/zsh/zshrc /etc/zshrc; do
  [ -f "$rc" ] || continue
  if grep -q '^# BEGIN unity-gateway' "$rc" && ! grep -q '^# END unity-gateway' "$rc"; then
    echo "$rc has a BEGIN unity-gateway line with no END line. Fix it and run this again." >&2
    exit 1
  fi
  sed -i '/^# BEGIN unity-gateway/,/^# END unity-gateway/d' "$rc"
  cat >>"$rc" <<'ALIASES'
# BEGIN unity-gateway (managed by MDM)
alias claude='UG_SKIP_OS_SETTINGS=1 ug claude'
alias codex='UG_SKIP_OS_SETTINGS=1 ug codex'
# END unity-gateway
ALIASES
done
