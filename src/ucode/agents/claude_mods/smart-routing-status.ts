import type { On } from 'claude-code'
import { SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY } from './constants.ts'

const BANNER = 'unity gateway smart router'

// The band shows at launch and after each toggle, and hides once the next prompt's turn
// completes. A prompt submitted while it shows arms the hide for that prompt's turn.
const band = { isVisible: true, isHideArmed: false }

export function showStatusBand(): void {
  band.isVisible = true
  band.isHideArmed = false
}

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

// isReplaced lets another above-prompt row take the band's place while it shows.
export const registerStatusBand = (on: On, isReplaced: () => boolean = () => false): void => {
  on('prompt.submit', ($, e, next) => {
    if (band.isVisible) band.isHideArmed = true
    return next(e)
  })

  on('turn.complete', ($, e, next) => {
    if (band.isHideArmed) {
      band.isVisible = false
      band.isHideArmed = false
      $.ui.invalidate('ui.render')
    }
    return next(e)
  })

  // The prompt hint row always carries the state as a plain tail, so it stays visible after
  // the band hides and flips as soon as a toggle succeeds.
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    const { enabled } = await readRoutingState($)
    const state = `smart router ${enabled ? 'ON' : 'OFF'}`
    const tail = e.props.tail ? `${e.props.tail} · ${state}` : state
    return next({ ...e, props: { ...e.props, tail } })
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const theirs = await next(e)
    if (!band.isVisible || isReplaced()) return theirs
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
