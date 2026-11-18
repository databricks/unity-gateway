# Codex Smart Router recordings

These recordings support the draft Smart Router footer PR. This media branch is separate from the feature branch so the videos do not enter the source change.

- `native-badges.mp4` (19 seconds): real Codex 0.171.0-alpha.12 terminal showing the final green ON and gray OFF badges, live ON/OFF/ON updates, and UNKNOWN/read-recovery behavior. This local UI-only check sends no model requests. Routing controls are changed through the public UG CLI; malformed input and repair exercise read recovery.
- `arclet-routing.mp4` (53 seconds): earlier real Arclet session against the requested Databricks workspace. A file read completes, then the installed Smart Router skill toggles OFF and ON in the same conversation. This recording predates the badge styling change and shows the earlier text-only indicator. Native one-time approval for the session-control write is visible.

Both MP4s render captured PTY output at its original timing, using a terminal font. No model responses or terminal states were fabricated. The workspace default model was rate limited during validation; the live recording uses GPT-6-Luna selected through normal user settings.
