# Claude Desktop CDP-only probe

Temporary Windows experiment, separate from the Cowork CI PR. Uses the same signed,
SHA256-pinned Claude Desktop 2.42304.2 installer and Playwright Core 1.62.1 from the
earlier Electron probe. Playwright is fetched through JFrog with its existing npm
integrity pin; no separate browser is installed.

First require a successful normal app launch, then stop all Claude processes and
launch with only `--remote-debugging-port=9222`. No Node inspector, alternate user
data directory, binary modifications, or inference credentials are used. If a CDP
endpoint appears, connect with `chromium.connectOverCDP` and inspect the app renderer.

Record normal-launch success, CDP endpoint availability, app exit, stderr, and
attachment outcome. An app refusal is a negative result, not evidence that all
Playwright configurations or other app versions/platforms are impossible.
