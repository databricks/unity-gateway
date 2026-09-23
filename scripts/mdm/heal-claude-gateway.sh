#!/usr/bin/env bash
#
# heal-ug-claude.sh
# Unblocks `ug claude` and bare-Claude gateway routing on a Mac left half-configured:
#   - removes the legacy ucode-claude-ide wrapper that hardcodes --provider
#   - clears the VS Code setting that points at that wrapper
#   - runs `ug configure` so it can write both its own settings and the OS-managed file
#   - verifies where each piece landed and reports what still needs attention
#
# RUN IT BY TYPING IT IN Terminal. Do NOT pipe it (no `curl ... | bash`) and do not
# redirect stdin: `ug configure` only writes the root-owned OS-managed settings when
# stdin is an interactive TTY, which is the step that makes bare `claude` / VS Code route.
#
# Usage:
#   bash heal-ug-claude.sh https://dbc-XXXXXXXX.cloud.databricks.com
#   # or:  UG_WORKSPACE_HOST=https://... bash heal-ug-claude.sh
#
# Re-running is safe. Everything it changes is backed up first.

set -uo pipefail   # deliberately not -e: run every check and report, don't abort on first failure

WORKSPACE_HOST="${1:-${UG_WORKSPACE_HOST:-}}"
TS="$(date +%Y%m%d-%H%M%S)"
WRAPPER="$HOME/.local/bin/ucode-claude-ide"
UCODE_SETTINGS="$HOME/.claude/ucode-settings.json"
MANAGED_DIR="/Library/Application Support/ClaudeCode"
MANAGED_FILE="$MANAGED_DIR/managed-settings.json"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[!]\033[0m %s\n' "$*" >&2; }
err()  { printf '  \033[31m[x] %s\033[0m\n' "$*" >&2; }

REPORT="$HOME/ug-heal-report-$TS.txt"

# Mask token-like strings before anything is written to the shareable report.
redact() {
  sed -E \
    -e 's/dapi[0-9a-f]+/<redacted-pat>/g' \
    -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/<redacted-jwt>/g' \
    -e 's/([Bb]earer )[A-Za-z0-9._-]+/\1<redacted>/g' \
    -e 's/("(token|secret|client_secret|refresh_token|access_token|password)"[[:space:]]*:[[:space:]]*")[^"]*/\1<redacted>/g'
}

# ---- preconditions ----------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || { err "This script targets macOS."; exit 1; }
if [ -z "$WORKSPACE_HOST" ]; then
  err "Pass your workspace URL, e.g.: bash $0 https://dbc-XXXXXXXX.cloud.databricks.com"
  exit 1
fi
case "$WORKSPACE_HOST" in https://*) ;; *) err "Workspace URL must start with https://"; exit 1 ;; esac
if [ ! -t 0 ]; then
  err "stdin is not an interactive terminal. ug would SKIP the OS-managed settings write"
  err "(so bare 'claude' / VS Code would not route), and its agent picker can hang."
  err "Re-run by TYPING this in Terminal, not piping it."
  exit 1
fi
command -v ug >/dev/null 2>&1 || { err "ug is not on PATH. Run 'ug upgrade' (or install ug) first."; exit 1; }
ok "ug $(ug --version 2>/dev/null || echo '(version unknown)')"
ok "workspace $WORKSPACE_HOST"

# ---- 1. remove the legacy wrapper that hardcodes --provider -----------------
# It is not shipped by ug; it forces `ug claude --provider ...`, which a managed
# config now rejects. Backed up, not deleted.
say "Legacy ucode-claude-ide wrapper"
if [ -e "$WRAPPER" ] || [ -L "$WRAPPER" ]; then
  mv "$WRAPPER" "$WRAPPER.bak.$TS" && ok "moved aside -> $WRAPPER.bak.$TS"
else
  ok "not present"
fi

# ---- 2. clear the VS Code setting that points Claude at that wrapper --------
# With the key gone, the extension launches Claude the default way, which routes
# through the OS-managed layer instead of the removed wrapper.
say "VS Code claudeCode.claudeProcessWrapper setting"
for base in "$HOME/Library/Application Support/Code/User" \
            "$HOME/Library/Application Support/Code - Insiders/User"; do
  s="$base/settings.json"
  [ -f "$s" ] || continue
  python3 - "$s" "$TS" <<'PY'
import json, shutil, sys
path, ts = sys.argv[1], sys.argv[2]
key = "claudeCode.claudeProcessWrapper"
try:
    with open(path) as fh:
        data = json.load(fh)
except Exception:
    # JSONC / comments / trailing commas: don't risk corrupting it.
    print(f"  [!] {path}: not strict JSON; if it sets {key}, remove that line by hand.")
    sys.exit(0)
if isinstance(data, dict) and key in data:
    shutil.copy2(path, f"{path}.bak.{ts}")
    data.pop(key, None)
    import os, tempfile
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)   # atomic swap so a crash never leaves settings.json truncated
    print(f"  [ok] removed {key} from {path} (backup {path}.bak.{ts})")
else:
    print(f"  [ok] {path}: key not set")
PY
done

# ---- 3. configure ug (interactive; approve the sudo prompt if asked) --------
# Writes ug's own gateway config to ~/.claude/ucode-settings.json (no sudo), and,
# on a TTY, the root-owned OS-managed managed-settings.json via sudo. Some orgs gate
# sudo behind an admin "reason" prompt; approve it when it appears. No --agent: the
# workspace's managed config drives which agents get configured (here, Claude only).
say "Running ug configure  (approve the sudo/admin prompt if it appears)"
CFG_LOG="$(mktemp -t ug-configure.XXXXXX)"
ug configure --workspace "$WORKSPACE_HOST" 2>&1 | tee "$CFG_LOG"
configure_rc="${PIPESTATUS[0]}"
[ "$configure_rc" -eq 0 ] && ok "ug configure exited cleanly" || warn "ug configure exited with code $configure_rc (read its output above)"

# ---- 4. verify where each piece landed --------------------------------------
say "Verification"

# ug's own config -> makes `ug claude` route (injected via --settings at launch)
if [ -f "$UCODE_SETTINGS" ] && grep -q "ANTHROPIC_BASE_URL" "$UCODE_SETTINGS" 2>/dev/null; then
  ok "ug config OK: $UCODE_SETTINGS has the gateway env -> 'ug claude' will route"
else
  warn "ug config missing gateway env at $UCODE_SETTINGS -> 'ug claude' may not route. Re-run ug configure."
fi

# OS-managed file -> makes bare `claude` and VS Code route
if [ -f "$MANAGED_FILE" ]; then
  ok "OS-managed settings present: $MANAGED_FILE -> bare 'claude' and VS Code will route"
else
  warn "No $MANAGED_FILE was written."
  warn "That means the privileged write was declined at the sudo/admin prompt, or the"
  warn "directory is locked by MDM. Bare 'claude' / VS Code then route ONLY if your MDM's"
  warn "managed-settings.d fragment carries the gateway env. Present drop-ins:"
  ls -1 "$MANAGED_DIR"/managed-settings.d/*.json 2>/dev/null | sed 's/^/      /' || echo "      (none found)"
  warn "Fix: re-run this script by typing it in Terminal and approve the prompt."
fi

# ---- 4b. write a shareable diagnostics report -------------------------------
# So a repeat failure can be triaged without another live session. Secrets are
# redacted; the file holds versions, ug configure output, and `ug doctor`.
say "Writing diagnostics report"
{
  echo "=== ug heal report  $TS ==="
  echo "workspace: $WORKSPACE_HOST"
  echo
  echo "=== system ==="
  uname -a
  sw_vers 2>/dev/null
  printf 'stdin_is_tty: %s\n' "$([ -t 0 ] && echo yes || echo no)"
  echo
  echo "=== ug ==="
  command -v ug 2>&1
  ug --version 2>&1
  echo
  echo "=== legacy wrapper ==="
  ls -l "$WRAPPER" "$WRAPPER.bak.$TS" 2>&1
  echo
  echo "=== ug configure output (exit $configure_rc) ==="
  [ -f "$CFG_LOG" ] && cat "$CFG_LOG" || echo "(no configure log)"
  echo
  echo "=== ug doctor ==="
  if command -v ug >/dev/null 2>&1 && ug doctor --help >/dev/null 2>&1; then ug doctor 2>&1; else echo "(ug doctor unavailable)"; fi
  echo
  echo "=== ~/.claude/ucode-settings.json ==="
  [ -f "$UCODE_SETTINGS" ] && cat "$UCODE_SETTINGS" || echo "(missing)"
  echo
  echo "=== OS-managed dir: $MANAGED_DIR ==="
  ls -la "$MANAGED_DIR" 2>&1
  printf 'managed-settings.json: %s\n' "$([ -f "$MANAGED_FILE" ] && echo present || echo MISSING)"
  echo "drop-ins:"; ls -1 "$MANAGED_DIR"/managed-settings.d/*.json 2>&1
} | redact > "$REPORT" 2>&1
[ -n "${CFG_LOG:-}" ] && [ -f "$CFG_LOG" ] && rm -f "$CFG_LOG"
ok "diagnostics written to $REPORT (secrets redacted)"

# ---- 5. next steps ----------------------------------------------------------
say "Next"
echo "  1. Run:  ug claude   (first launch opens a browser for Databricks OAuth; that is expected)"
echo "  2. Then test bare 'claude' and Claude in VS Code."
echo "  3. If bare 'claude' still bypasses the gateway, the OS-managed file above is the gap:"
echo "     re-run in Terminal and approve the admin prompt, or have your MDM own that file."
echo
echo
echo "  If anything above failed, send this file to the Databricks team: $REPORT"
echo "  Backups written this run carry the suffix .bak.$TS"
