# Claude delegated-work status

An interactive smart-routed Claude launch reports each delegated agent through
OSC 7501. Every agent has its own stable task ID, title, and status. A supporting
terminal decides how to display those tasks and combine them with Claude's main
turn. One child finishing or failing does not change another child's report.

The Claude extension samples its agent registry once per second and writes an
immutable conversation snapshot in the launch's temporary directory. One
launcher-owned worker publishes the changed records in its own namespace. It
starts after Claude is spawned and decodes the file only when it changes.

| Claude registry state | OSC 7501 report |
| --- | --- |
| Pending, Running | Working |
| Waiting | Blocked |
| Idle | Idle |
| Completed | Done |
| Failed | Error |
| Killed, or removed from the registry | Clear that agent's record |

Waiting is reported as Blocked because that agent cannot currently proceed. It
can be waiting for background work, a nested Agent call, or approval. The registry
does not supply that distinction, so Waiting alone leaves the reason unspecified.
When Claude displays an `AskUserQuestion` dialog for a subagent, its record instead
includes `state=blocked:kind=question`. This lets a terminal distinguish a known
request for an answer from a task that is blocked for an unspecified reason.
The question is matched by its tool-call ID, and answering, cancelling, or ending
the conversation clears that reason. Automatically answered calls do not acquire
it. The regular one-second sampler publishes these changes without doing I/O in
the renderer or altering the question's behavior.

Permission prompts remain unspecified: Claude's permission hook can be answered
automatically and does not expose the lifetime of the user's approval dialog.
This bridge therefore does not label a permission check as confirmed user input.
The terminal decides how to present Blocked and combine it with other agents'
states. Claude's own reports remain intact. Agent IDs are encoded into valid,
distinct OSC paths, and titles are encoded within the protocol's text limits.

Conversation replacement and launcher exit clear only UG's namespace. Failed or
incomplete reads retain the previous statuses and remain retryable. Headless
launches, redirected output, and `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` do not
publish reports. This applies to new launches and does not modify an agent that
is already running.

Native-extension tests cover individual registry states, the main turn finishing
before its children, simultaneous questions, automatic answers, cancellation,
resets and delayed replies. Python component tests cover input-reason changes,
independent completion, failure, cancellation and removal, stable identities,
valid OSC bytes, shutdown and launch isolation. These checks do not establish a
live model or ArcaTerm integration pass.
