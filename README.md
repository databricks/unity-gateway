# Codex Smart Router feedback TUI demo

[Watch the native selection flow](./codex-smart-router-feedback-menu.mp4).

30-second replay of actual Codex 0.167.2 terminal screens from one Arclet session for
[#1751](https://github.com/databricks/unity-gateway/pull/1751).

Shows the native Provide a reason / Cancel menu, free-text entry, short-reason
validation, a valid unsubmitted draft, Escape, and cancellation with routing still on.
No feedback report was captured or uploaded in this session. No shell approval.

[Earlier delivery verification](./codex-smart-router-feedback.mp4) preserves the
47-second replay from the previous inline-chat UI. It shows valid-reason capture,
routing off, same-report retry, backend confirmation, and continued work. Separate
cancel, submit, and resumed retry sessions are joined and labeled. The isolated test
home initially lacked dbcert credentials; credentials were restored before retrying
the same saved report. One authorized test report reached Isaac's backend.

Both videos replay saved native terminal screens with onboarding cropped, blank
gaps collapsed, and pauses shortened. No product output is invented. Backend
submission has not been repeated through the new selector.

This media-only branch keeps videos out of the feature PR's code diff.
