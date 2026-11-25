# Codex Smart Router footer

The terminal footer uses the experimental MCP status-line provider API first
shipped in stable Codex `0.171.0`. UG registers a local stdio provider for each
routed launch and selects it in the terminal client. Both configurations are
launch-only. The provider needs no gateway credentials or network access.
Its MCP tool is visible only to the app, so it is excluded from model-facing tool declarations.

The indicator reflects the latest session controls, rather than a turn's original
routing attribution. Existing children and an ongoing main turn keep their original
attribution after a toggle. A failed controls read shows `UNKNOWN` without stopping
Codex's polling, so the next successful read can recover.

The state badge matches Claude's Smart Router banner: bold black `ON` on green,
or bold black `OFF` on gray. Claude keeps an ON/OFF hint below the prompt after its
temporary banner disappears; disabling routing does not hide that hint. Codex keeps
the badge in its persistent footer, since this API only controls the footer.

While the startup check runs (up to 11 s), the footer shows `smart router` without a badge.
When the check turns routing off, the badge is bold black `ERROR` on yellow, followed by
Claude's unsupported-list warning or, after a failed check, `⚠ Smart Router couldn't validate
this session's model list`, and then `($smart-router logs for details)`. The warning hides
once the first prompt sent while it shows completes. The badge and the hint stay until the
next `$smart-router on` or `off`.

`$smart-router on` runs the check before it turns routing on, including when routing is
already on. A supported list or an HTTP 404 turns routing on with no warning. An unsupported
list or a failed check keeps routing off, shows the `ERROR` badge with its warning and the
logs hint, writes a logs row immediately, and the command reply says routing is off.
`$smart-router off` clears any warning. A missing or unreadable check result leaves the
plain footer.

## Validation

Install the checkout in an isolated Python environment and use Codex
`0.171.0` on that environment's PATH. Do not replace the user's
installation as part of automated tests. Check both `codex --version` and `ug --version`.

Run the offline checks:

```bash
uv run pytest tests/test_codex_statusline.py tests/test_codex_smart_routing_v2.py \
  tests/test_codex_metadata.py tests/test_smart_router_feedback.py \
  tests/test_smart_router_logs_cli.py tests/test_route_validation.py tests/test_agent_codex.py \
  tests/test_smart_router.py \
  tests/test_smart_routing_config.py tests/test_integration_contract.py
just lint
```

For a configured test workspace and an explicitly selected profile:

1. Launch `SMART_ROUTER_CONFIG_VERSION=subagent_only_v0 ug codex`. Check the
   native footer shows `smart router` with a green `ON` badge alongside model and project directory.
2. Ask Codex to read a file containing an unpredictable value. Require the correct
   value in its completed answer, rather than accepting a startup banner as inference.
3. Submit `$smart-router off`. After the command succeeds, require a gray `OFF`
   badge in the footer without restarting the conversation. The indicator stays visible.
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
9. With an older client and explicit routing selected, verify the upgrade gate blocks
   routed launch until the upgrade succeeds. With managed routing and no explicit
   selection, decline the upgrade and require a warning, a completed task, and no footer.
   `--disable-smart-routing` retains ordinary launch behavior.

Existing native routing journeys can be run with pinned stable Codex through
`scripts/run_integration.py --ug-version checkout --codex-version 0.171.0
--profile <selected-profile> --workspace <test-workspace> -- -k routing_toggle`.
Those journeys verify inference and session continuity; the footer assertions above
are additional manual UI checks.

## Stable validation

On 2026-11-20, native Linux Codex `0.171.0` passed ON/OFF/ON badge rendering and
UNKNOWN/read recovery. An isolated-wheel Arclet run against the supplied workspace
completed five hidden-file tasks across two concurrent sessions and a first-prompt
launch, with installed-skill toggles, unchanged conversation identity, independent
controls, and unchanged saved footer settings and UG profile. GPT-6-Luna was selected
through ordinary user settings. Seven installation checks passed.

The initial first-prompt routing check returned HTTP 400/downstream 422 and fell
back; its delegated file tasks completed without a recorded routing decision.
Later live Arclet ON/OFF/ON and managed-routing journeys passed with correlated
child-model selection. A separate native plaintext journey verified the exact
assignment and digest at the hook and child. Native Linux and Windows routing CI
subsequently passed. Manual badge-rendering validation remains Linux-only.

## Required upgrade

Routed launches require stable Codex `0.171.0` or newer. Earlier builds, including
`0.171.0-alpha.*`, must pass the existing native upgrade flow before starting.
Declining or failing the upgrade blocks routed launch; `--disable-smart-routing`
keeps ordinary launch behavior. `UG_AUTO_UPGRADE_AGENT=1` selects automatic upgrade.
When routing comes from the managed default, an unsuccessful upgrade instead warns
and continues without routing or the footer, with no extra launch flag required.
The footer is registered after that launch gate, without a second version probe.

Upstream contract and starter:
[create-statusline](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/skills/src/assets/samples/create-statusline/SKILL.md).
