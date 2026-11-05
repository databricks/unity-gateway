# macOS Cowork feasibility probe

Independent experiment on disposable GitHub-hosted `macos-15` (Apple Silicon) and
`macos-15-intel` runners. This is
not part of the required integration gate. Pushes to the probe branch run it.

Installs the approved official Claude Desktop 4.23118.0 universal archive,
verifies SHA256 and its Anthropic signing identity, and installs the current UG
checkout and Claude CLI 2.1.290 through internal JFrog registries. No virtualization
setup or preliminary Desktop launch is performed. A read-only diagnostic records
whether Apple’s Virtualization framework is supported. Disabled controls fail the
probe before attempting inference.

Uses existing CI workspace/bearer credentials and `ug configure` with an explicit
`desktop-ci` profile. AppleScript/System Events drives actual Cowork controls and
requires the selected `system.ai.claude-haiku-4-5` model, the expected assistant
answer, and a completed response. The prompt spells the number in words, so its
echo cannot satisfy the numeric answer assertion.

Artifacts expire after three days: sanitized UI snapshots, native logs,
configuration status, installer provenance, and a desktop screenshot. No app
profiles or credential files are uploaded. Screen capture assumes this fresh
runner displays only the test app. This probe does not exercise tools, file
operations, or Cowork VM readiness.
