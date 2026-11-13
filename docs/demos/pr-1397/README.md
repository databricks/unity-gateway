# Claude prompt recovery recording

[Watch the 39-second recording](claude-prompt-recovery.mp4).

Native Claude Code 2.1.322 runs the production PTY wrapper and saved-model guard from
`367b2ff762720b417dfc12ab085dfcf82cd47e7c` on Linux. The video renders its actual
terminal output and sampled settings at five frames per second. Onboarding is
omitted; the recovery sequence runs at its original speed, followed by a
three-second hold of the final frame.

The native process pauses for 7.501 seconds while the automatic `/model haiku`
command is pending, crossing the unchanged six-second recovery deadline. After
Claude completes the command, UG restores the saved Sonnet default and replays
`Keep this exact prompt after the delayed model switch.` The native submission
hook receives exactly the same text. A later manual `/model opus` saves Opus, and
Opus remains saved after normal exit and guard cleanup.

The route selection is a fixed offline fixture. The process has a fresh home and
runs in a network-disabled namespace; the submission hook blocks inference after
confirming receipt. This covers native Linux prompt and settings recovery, not
live gateway routing or model responses.

Video SHA-256:
`89870fdbba04f4dab9f96f29702735d4821e91faaca646b55626f6cb6f3c4d49`

Recorded on November 13, 2026. The MP4 is 337,565 bytes, H.264, 1680 x 1000, and
39.2 seconds. Product source is unchanged by this recording addition.
