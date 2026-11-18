set -euo pipefail
test "$GITHUB_ACTIONS" = true
test "$(uname -s)" = Darwin
npm install -g '@anthropic-ai/claude-code@2.1.290' --no-audit --fund=false \
  --registry https://registry.npmjs.org --fetch-retries=0 --fetch-timeout=30000
claude --version
runtime="$RUNNER_TEMP/macos-desktop-runtime"
python -m venv "$runtime"
PIP_CONFIG_FILE=/dev/null PIP_EXTRA_INDEX_URL='' "$runtime/bin/python" -m pip install \
  --disable-pip-version-check --index-url https://pypi.org/simple .
echo "UG_COWORK_PYTHON=$runtime/bin/python" >> "$GITHUB_ENV"
# UG writes its own path into Desktop's MCP connectors from PATH, as for an installed ug.
echo "$runtime/bin" >> "$GITHUB_PATH"
