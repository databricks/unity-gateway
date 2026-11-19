# Codex Smart Router feedback TUI validation

[Watch final Arclet validation](./codex-smart-router-final.mp4).

60-second replay of actual Codex 0.167.2 terminal screens for
[#1751](https://github.com/databricks/unity-gateway/pull/1751).

Shows the native reason / Cancel menu, text entry, short-reason rejection without
reopening, explicit retry, Cancel/Escape, valid-reason submission, routing off,
backend completion, continued work, re-enable, Omnigent exemption, and saved-report retry.
No shell approval or sandbox error. One explicitly authorized test report reached Isaac.

The full submission ran on f0a90f83. Final scenes repeat prompt, short input,
saved-report delivery, and Omnigent checks on review-fix head a2fd990a. Final-head
checks also passed Cancel/Escape and re-enable. They reused the same saved report;
no second report was created and its original receipt stayed unchanged.

The replay joins labeled sessions, crops onboarding, and shortens pauses.
Product output comes from captured native terminal screens. Source capture hashes,
scene timestamps, tested commits, and video hash are in
[final-video-manifest.json](./final-video-manifest.json).

[Earlier native-menu demo](./codex-smart-router-feedback-menu.mp4) and
[earlier inline-chat demo](./codex-smart-router-feedback.mp4) remain for reference.
This media branch keeps videos out of the feature PR's code diff.
