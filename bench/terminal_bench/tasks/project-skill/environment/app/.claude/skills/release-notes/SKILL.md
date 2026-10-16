---
name: release-notes
description: Build release notes for this project. Use whenever someone asks for release notes or a changelog.
---

# Release notes

1. Run `python .claude/skills/release-notes/build.py <version>`. It reads `changes.txt` and prints a draft.
2. Save the draft to `dist/release-notes-<version>.md`.
3. Add this as the last line of the file, exactly: `Signed-off-by: release-bot`
