# Native Claude session settings: runtime control and remaining adapter work

Claude has an in-memory settings update API. The published TypeScript Agent SDK
`Query.applyFlagSettings(settings)` merges settings into the session's flag layer and
returns a promise for the control response. It is available only in streaming input mode.
The flag layer takes precedence over user/project/local settings and remains below managed
policy. This API does not write a settings file.

The declaration was checked in `@anthropic-ai/claude-agent-sdk` 0.3.287
([published SDK types](https://unpkg.com/@anthropic-ai/claude-agent-sdk@0.3.287/sdk.d.ts)).
The equivalent native control request was tested with Claude Code 2.1.286:

```json
{
  "type": "control_request",
  "request_id": "unique-update-id",
  "request": {
    "subtype": "apply_flag_settings",
    "settings": {
      "env": {
        "CLAUDE_CODE_EXTRA_BODY": "{\"session_test_value\":\"updated\"}"
      }
    }
  }
}
```

Wait for the matching successful `control_response` before submitting the next turn.
Successive calls shallow-merge top-level settings: an `env` update replaces the flag
layer's previous `env` object. A production caller must preserve its existing environment
entries and merge unrelated extra-body fields before sending the update.

This keeps `CLAUDE_CODE_EXTRA_BODY` as Claude's body composition mechanism while removing
settings-file writes and watcher timing from the update path. The controller can keep the
current value in memory, scoped to each running session. It does not modify requests already
constructed or in flight.

## Reproducible native component checks

Select these checks explicitly; they are outside default pytest collection. Supply an
absolute executable path and its exact version. Missing prerequisites fail this selected suite.
The fixture API directly answers native requests and invokes a native Agent child. It does
not forward traffic or contact the Databricks gateway. Only sanitized request fields are captured.

```bash
export UCODE_TEST_CLAUDE_BINARY=/absolute/path/to/claude
export UCODE_TEST_CLAUDE_VERSION=2.1.286
uv run pytest tests/native_claude/test_session_settings.py -v
```

The runtime-control case starts two native sessions without any settings files, changes one
through initial → updated → initial using acknowledged control requests, and checks both
main-agent and native Agent child request bodies immediately after each acknowledgement.
The second session keeps its separate value. Existing caller body fields survive, and neither
the startup settings file nor a watched user settings file is created. No sleep or watcher is
used for this case. Normal Claude state/history writes are outside the claim.

Three comparison cases cover watched user settings, cached startup `--settings`, and absent
extra-body configuration. The watched case uses a one-second delay and previously failed
with a stale first parent request before child/parent requests adopted the update. It passed
in the latest run without changes to that assertion or delay, confirming that the prior
failure was timing-dependent. The latest full native run had **4 passes**; that does not turn
the watcher into an acknowledged update mechanism.

These are native component checks, not UG launch integration, interactive slash-command
coverage, or live gateway routing coverage. The normal helper tests run with:

```bash
uv run pytest tests/test_session_settings.py tests/test_claude_session_settings.py
```

## Other mechanisms checked

- Launch-time `CLAUDE_CODE_EXTRA_BODY` can set a different value for each session without
  settings files, but cannot be mutated by an independent hook process.
- `update_environment_variables` is restricted to two authentication-token variables in
  Claude 2.1.286; it is not a general environment update API.
- `updateSettings` writes allowlisted keys to user/local files. It is distinct from the
  in-memory `applyFlagSettings` API.
- The ordinary hooks reviewed expose prompt/tool lifecycle events, not a callback to edit
  each inference request body.

## Remaining production adapter work

The verified control path uses `--print --input-format stream-json --output-format stream-json`.
UG's current interactive PTY launcher does not own such a control channel. A hook subprocess
cannot send this message to an ordinary terminal session by exporting an environment variable.
No supported way to attach this arbitrary-settings control to the current normal TUI launch
has been verified. The SDK method alone is therefore not a drop-in fix for that launcher.

For SDK/streaming launches, use acknowledged `applyFlagSettings` updates. Before adopting it
for the interactive UG flow, establish a supported control connection to that same session,
preserve the normal terminal UI, auth, hooks, and user/project settings, and test managed
precedence and in-flight behavior. No production launcher was changed by this investigation.

The general JSON store remains available to consumers that need cross-process file state,
but it is not required to update Claude's extra body through the verified runtime API.
