# Claude live workflow recording

[Watch the 49-second recording](claude-live-workflow.mp4).

Native Claude Code 2.1.322 runs `ug claude --enable-smart-routing` from
`c9119e0f4bd13a1e80ef6bb79b783c983d83e21a` on Linux with
`SMART_ROUTER_CONFIG_VERSION=first_prompt_and_subagent_no_orch_v0`.
The live gateway router selects Sonnet 5.5, UG switches the native model and
replays the original prompt, and Sonnet answers that `len([2, 4, 6])` returns `3`.
UG restores the saved Opus default after the automatic switch.

A manual `/model haiku` selects Haiku 5.5. Its live response to `12 + 30` is `42`,
and `haiku` remains saved after normal exit with code 0 and recovery cleanup.
Native submission records match both entered prompts exactly. No timeout occurred
in this run; delayed recovery remains covered by the component tests.

The native picker offers the discovered Fable 5.2, Opus 5.5, Sonnet 5.5 and
Haiku 5.5 defaults. The earlier 31-row catalog exceeded the router's 20-service
limit; that attempt and its live fallback response are preserved separately.
Existing workspace authentication and normal networking are used. The private
configuration preserves the original managed policies. No route, response,
delay or failure is simulated.

The video renders actual terminal output and sampled native settings at five
frames per second. Setup and idle inspection time are trimmed; each interaction
runs at its original speed, with a three-second final hold. Recorded on November
13, 2026: H.264, 1680 x 1000, 49.0 seconds, 738,056 bytes.

Video SHA-256:
`9960a848d16a76a28f215d5e93fe0ff487f6dbd8a95037bb900ddda7373a1215`

Product and test files are unchanged. The prior 296-test result is retained
without a rerun for this documentation change.

The [earlier offline recording](claude-prompt-recovery.mp4) remains as historical
evidence. It used a fixed route, an injected delay and disabled inference, so it
does not satisfy the live workflow requirement. Its original SHA-256 remains
`89870fdbba04f4dab9f96f29702735d4821e91faaca646b55626f6cb6f3c4d49`.
