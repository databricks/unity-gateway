import type { On } from 'claude-code'
import { SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY } from './constants.ts'

const BANNER = 'unity gateway smart router'

type RoutingState = { enabled: boolean; firstPrompt: boolean }

// Claude's capability scanner requires literal names in $.env.get calls.
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
    enabled: full === '1' || subagentOnly === '1',
    firstPrompt: full === '1' && subagentOnly !== '1',
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
