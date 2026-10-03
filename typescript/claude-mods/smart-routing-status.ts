import type { Register } from 'claude-code'

type On = Parameters<Register>[0]

// The status band concern of ug's smart-routing UI mod (composed by register.ts).
// Draws a band above the prompt showing whether smart routing is on and what it
// routes. The "on" pill uses a bright background with black text so it reads in
// both light and dark terminals (Claude Code's theme keys color foreground text,
// not pill backgrounds). The env var names and truthy vocabulary below are kept
// in sync with ucode.constants / ucode.smart_routing.v2 (a test guards it).

const SMART_ROUTING_KEY = 'ENABLE_SMART_ROUTING_V2'
const SUBAGENT_ONLY_KEY = 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY'
const SESSION_FILE_KEY = 'UCODE_SESSION_ENV_FILE'
const BANNER = 'unity gateway smart router'
const TRUTHY = ['1', 'true']

const truthy = (value: unknown): boolean =>
  typeof value === 'string' && TRUTHY.includes(value.trim().toLowerCase())

type RoutingState = { enabled: boolean; firstPrompt: boolean }

// Mirror ucode.smart_routing.v2.smart_routing_enabled / first_prompt_routing_enabled.
// A hooks module has no Node APIs: env and files come through the mods API, and
// $.env.get must be called with a string literal.
async function readRoutingState($: any): Promise<RoutingState> {
  let full: unknown
  let subagentOnly: unknown
  let file: unknown
  try {
    full = await $.env.get('ENABLE_SMART_ROUTING_V2')
  } catch {}
  try {
    subagentOnly = await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY')
  } catch {}
  try {
    file = await $.env.get('UCODE_SESSION_ENV_FILE')
  } catch {}
  if (typeof file === 'string' && file) {
    try {
      const overrides = JSON.parse(await $.fs.read(file))
      if (overrides && typeof overrides === 'object') {
        if (SMART_ROUTING_KEY in overrides) full = overrides[SMART_ROUTING_KEY]
        if (SUBAGENT_ONLY_KEY in overrides) subagentOnly = overrides[SUBAGENT_ONLY_KEY]
      }
    } catch {}
  }
  return {
    enabled: truthy(full) || truthy(subagentOnly),
    firstPrompt: truthy(full) && !truthy(subagentOnly),
  }
}

export const registerStatusBand = (on: On): void => {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const theirs = await next(e)
    const { Box, Text } = $.ui.resolve(e)
    const { enabled, firstPrompt } = await readRoutingState($)

    const pill = enabled
      ? Text({ bold: true, color: 'black', backgroundColor: 'green', children: [' ON '] })
      : Text({ bold: true, color: 'black', backgroundColor: 'gray', children: [' OFF '] })
    const trailer = enabled
      ? Text({
          dimColor: true,
          italic: true,
          children: ['— ' + (firstPrompt ? 'routing first prompt + subagents' : 'routing subagents')],
        })
      : Text({ dimColor: true, children: ["— Reenable with '/smart-router on'"] })

    const line = Box({
      flexDirection: 'row',
      columnGap: 1,
      children: [Text({ bold: true, children: [BANNER] }), pill, trailer],
    })
    const children = theirs ? [line, theirs] : [line]
    return Box({ flexDirection: 'column', children })
  })
}
