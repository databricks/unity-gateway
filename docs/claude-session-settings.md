# Native Claude session settings: evidence and remaining adapter work

The shared store in `ucode.session_settings` can represent ordinary Claude settings,
including `env`, without depending on Smart Router. `agents.claude_settings.merge_extra_body`
merges arbitrary owned request fields into `CLAUDE_CODE_EXTRA_BODY` while preserving caller
fields. Neither helper alone changes a running Claude client's settings source.

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

The cases check:

- Watched user settings update main and child payloads through initial → updated → initial,
  while a second concurrently running session keeps its own value and caller fields.
- Replacing the startup file supplied through `--settings` remains cached on Claude 2.1.286.
- Absent extra-body configuration adds no fixture payload field.

Each watched update is atomic and followed by a one-second wait. The stronger two-session
check exposed a timing gap: a main-agent request still sent the old value, followed by child
and parent requests with the new value. That assertion remains in the test. Earlier single-session
probes passed, so those probes establish hot-reload capability rather than a reliable
post-toggle propagation guarantee. The selected native run had one failure and two passes.

These are native component checks, not UG launch integration, interactive slash-command
coverage, or live gateway routing coverage. The normal helper tests run with:

```bash
uv run pytest tests/test_session_settings.py tests/test_claude_session_settings.py
```

## Remaining production adapter work

This draft does not redirect production Claude configuration. Before it can satisfy
session-local native reload requirements, the adapter must:

1. Supply a watched source private to each launch without changing the user's auth,
   history, skills, project settings, hooks, or configuration scope.
2. Handle settings precedence, including managed and caller-provided startup settings.
3. Establish when the native client has applied an update before claiming a toggle is complete.
4. Pass native main/child and concurrent-session checks through the actual UG launcher,
   including preservation of existing user/project configuration.

The separate Smart Router draft consumes the shared storage and body helper. Its native
off/on payload criteria remain dependent on this unfinished adapter.
