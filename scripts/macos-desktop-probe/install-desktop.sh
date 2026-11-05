set -euo pipefail
test "$GITHUB_ACTIONS" = true
test "$(uname -s)" = Darwin
evidence="$RUNNER_TEMP/macos-cowork-evidence"
mkdir -p "$evidence"
archive="$RUNNER_TEMP/Claude.zip"
curl --fail --location --retry 2 --max-time 300 \
  'https://downloads.claude.ai/releases/darwin/universal/4.23118.0/Claude-8175c354eda704cea00fdeddfa69cea329dd321a.zip' \
  --output "$archive"
echo "5b8feee3da048e26bbb29a5d601e97034f674ac3b76f7d0237751c46d6034b23  $archive" | shasum -a 256 --check
/usr/bin/ditto -x -k "$archive" /Applications
/usr/bin/codesign --verify --deep --strict /Applications/Claude.app
/usr/bin/codesign -dv --verbose=4 /Applications/Claude.app 2> "$evidence/signature.txt"
grep -Fx 'TeamIdentifier=Q6L2SF6YDW' "$evidence/signature.txt"
/usr/sbin/spctl --assess --type execute --verbose=2 /Applications/Claude.app
/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' /Applications/Claude.app/Contents/Info.plist > "$evidence/version.txt"
test "$(cat "$evidence/version.txt")" = 4.23118.0
sw_vers > "$evidence/os.txt"
uname -m >> "$evidence/os.txt"
/usr/bin/swift -e 'import Virtualization; print("VZVirtualMachine.isSupported=\(VZVirtualMachine.isSupported)")' > "$evidence/virtualization.txt"
/usr/bin/swiftc scripts/macos-desktop-probe/ui.swift -o "$RUNNER_TEMP/desktop-ui"
