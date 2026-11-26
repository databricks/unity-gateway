# Claude delegated-work status

An interactive smart-routed Claude launch reports outstanding delegated work through
OSC 7501. This keeps a supporting terminal's tab Working when Claude's main turn
has ended but its subagents are still working. No screen parsing is involved.

The Claude extension samples its agent registry once per second and writes a small
conversation snapshot in the launch's temporary directory. The launcher owns one
separate OSC task for this delegated work. Pending, running and waiting agents
keep that task Working. Waiting can mean background work, so it does not by itself
produce a Needs Input report. Claude's own reports continue to supply input requests
and other task details.

The task reports Done after observed outstanding work reaches zero. Conversation
replacement and launcher exit clear only UG's task. Failed reads retain the last
known state and remain retryable. Headless launches, redirected output and
`CLAUDE_CODE_DISABLE_TERMINAL_TITLE` do not publish reports. The bridge belongs to
new launches and does not modify an agent that is already running.

This reports delegated work as one group. It does not claim separate subagent
outcomes or permission reasons. Native-extension tests cover registry sampling,
main-turn completion, resets and delayed replies. Python component tests cover the
OSC bytes, transition rules, shutdown and launch isolation. These checks do not
establish a live model or ArcaTerm integration pass.
