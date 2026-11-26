set -euo pipefail
test "$GITHUB_ACTIONS" = true
test "$(uname -s)" = Darwin
"$RUNNER_TEMP/cowork-claude-2.1.290/node_modules/.bin/claude" --version
echo "$RUNNER_TEMP/cowork-claude-2.1.290/node_modules/.bin" >> "$GITHUB_PATH"
runtime="$RUNNER_TEMP/macos-desktop-runtime"
PIP_CONFIG_FILE=/dev/null PIP_EXTRA_INDEX_URL='' "$runtime/bin/python" -m pip install \
  --disable-pip-version-check --index-url https://pypi.org/simple .
echo "UG_COWORK_PYTHON=$runtime/bin/python" >> "$GITHUB_ENV"
# UG writes its own path into Desktop's MCP connectors from PATH, as for an installed ug.
echo "$runtime/bin" >> "$GITHUB_PATH"
