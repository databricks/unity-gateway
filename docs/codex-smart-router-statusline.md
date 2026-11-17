# Codex Smart Router footer

The terminal footer uses the experimental MCP status-line provider API first
available in Codex `0.171.0-alpha.12`. UG registers a local stdio provider for each
routed launch and selects it in the terminal client. Both configurations are
launch-only. The provider needs no gateway credentials or network access.

The indicator reflects the latest session controls, rather than a turn's original
routing attribution. Existing children and an ongoing main turn keep their original
attribution after a toggle. A failed controls read shows `UNKNOWN` without stopping
Codex's polling, so the next successful read can recover.

## Validation

Install the checkout in an isolated Python environment and use Codex
`0.171.0-alpha.12` on that environment's PATH. Do not replace the user's stable
installation as part of automated tests. Check both `codex --version` and `ug --version`.

Run the offline checks:

```bash
uv run pytest tests/test_codex_statusline.py tests/test_codex_smart_routing_v2.py \
  tests/test_codex_metadata.py tests/test_agent_codex.py tests/test_smart_router.py \
  tests/test_smart_routing_config.py tests/test_integration_contract.py
just lint
```

For a configured test workspace and an explicitly selected profile:

1. Launch `SMART_ROUTER_CONFIG_VERSION=subagent_only_v0 ug codex`. Check the
   native footer shows `smart router ON` alongside model and project directory.
2. Ask Codex to read a file containing an unpredictable value. Require the correct
   value in its completed answer, rather than accepting a startup banner as inference.
3. Submit `$smart-router off`. After the command succeeds, require `smart router OFF`
   in the footer without restarting the conversation.
4. Submit `$smart-router on` and require `smart router ON` again. Complete another
   file task in the same conversation.
5. Open a second session. Toggle one and check the other session's footer is unchanged.
6. Repeat with `first_prompt_and_subagent_no_orch_v0` and complete an initial prompt.
7. Repeat on Windows. Confirm both the app-server and remote terminal receive the
   correct configuration, including when paths contain spaces.
8. Compare saved footer preferences and MCP registrations before and after.
   Codex may add its own trust and onboarding records, such as
   `tui.screen_reader_detection_done`; compare `tui.status_line` and
   `tui.status_line_provider` independently of that bookkeeping.
9. On stable `0.169.0`, confirm routed launch still works with its existing footer.

Existing native routing journeys can be run with pinned alpha through
`scripts/run_integration.py --ug-version checkout --codex-version 0.171.0-alpha.12
--profile <selected-profile> --workspace <test-workspace> -- -k routing_toggle`.
Those journeys verify inference and session continuity; the footer assertions above
are additional manual UI checks.

## Stable rollout

Keep the feature PR in draft while testing the alpha. Do not force users onto an
experimental Codex release. Before delivery, identify the first stable release
containing the provider API, rerun protocol, UI, and routed inference checks on that
exact build, then update UG's minimum Codex version and its existing upgrade tests.
Version `0.171.0` is a candidate, not a confirmed stable release target.

Upstream contract and starter:
[create-statusline](https://github.com/openai/codex/blob/rust-v0.171.0-alpha.12/codex-rs/skills/src/assets/samples/create-statusline/SKILL.md).
