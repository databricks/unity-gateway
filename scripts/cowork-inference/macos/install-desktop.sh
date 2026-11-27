set -euo pipefail
test "$GITHUB_ACTIONS" = true
test "$(uname -s)" = Darwin
# The workflow env pins the archive and the cache key together, so bumping one bumps both.
: "${CLAUDE_DESKTOP_MACOS_URL:?Set CLAUDE_DESKTOP_MACOS_URL to the pinned Claude Desktop archive}"
: "${CLAUDE_DESKTOP_MACOS_SHA256:?Set CLAUDE_DESKTOP_MACOS_SHA256 to the SHA256 of the pinned archive}"
evidence="$RUNNER_TEMP/macos-cowork-evidence"
mkdir -p "$evidence"
archive="$RUNNER_TEMP/Claude.zip"
if [ ! -f "$archive" ]; then
  curl --fail --location --retry 2 --max-time 300 \
    "$CLAUDE_DESKTOP_MACOS_URL" \
    --output "$archive"
fi
echo "$CLAUDE_DESKTOP_MACOS_SHA256  $archive" | shasum -a 256 --check
/usr/bin/ditto -x -k "$archive" /Applications
/usr/bin/codesign --verify --deep --strict /Applications/Claude.app
/usr/bin/codesign -dv --verbose=4 /Applications/Claude.app 2> "$evidence/signature.txt"
grep -Fx 'TeamIdentifier=Q6L2SF6YDW' "$evidence/signature.txt"
/usr/sbin/spctl --assess --type execute --verbose=2 /Applications/Claude.app
/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' /Applications/Claude.app/Contents/Info.plist > "$evidence/version.txt"
test "$(cat "$evidence/version.txt")" = 4.23118.0
sw_vers > "$evidence/os.txt"
uname -m >> "$evidence/os.txt"
/usr/bin/swiftc scripts/cowork-inference/macos/ui.swift -o "$RUNNER_TEMP/desktop-ui"
