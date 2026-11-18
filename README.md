# Codex Smart Router feedback TUI demo

[Watch or download the MP4](./codex-smart-router-feedback.mp4).

47-second replay of actual Codex 0.167.2 sessions on Arclet for
[#1751](https://github.com/databricks/unity-gateway/pull/1751).

Shows inline feedback disclosure, short-reason validation, cancellation with routing
still on, a valid reason followed by routing off, same-report native retry, backend
confirmation after the model's final reply, and continued work. No shell approval.

Separate cancel, submit, and resumed retry sessions are joined and labeled. The
isolated test home initially lacked dbcert credentials; credentials were restored
before retrying the same saved report. One clearly labeled test report reached
Isaac's backend. No additional report was captured during retries.

Saved native terminal screens are replayed with onboarding cropped, blank gaps
collapsed, and pauses shortened. No product output is recreated.

This media-only branch keeps the video out of the feature PR's code diff.
