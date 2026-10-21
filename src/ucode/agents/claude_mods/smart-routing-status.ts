import type { On } from 'claude-code'
import { SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY } from './constants.ts'
import { parseSessionSwitch } from './session-file.ts'

const BANNER = 'unity gateway smart router'

// Once the session has been on, the band shows at launch and after each toggle, and hides once
// the next prompt's turn completes. A prompt submitted while it shows arms the hide for that turn.
const band = { isVisible: true, isHideArmed: false }

export function showStatusBand(): void {
  band.isVisible = true
  band.isHideArmed = false
}

type RoutingState = { enabled: boolean; firstPrompt: boolean }

// Undefined until the session has been on: before that it shows no Smart Router UI.
async function readRoutingState($: any): Promise<RoutingState | undefined> {
  let session
  try {
    // Claude's capability scanner requires literal names in $.env.get calls.
    const file = await $.env.get('UCODE_SESSION_ENV_FILE')
    session = file ? parseSessionSwitch(await $.fs.read(file)) : undefined
  } catch {}
  if (!session?.everEnabled) return undefined
  return {
    enabled: session.enabled,
    firstPrompt: session.whenOn[SMART_ROUTING_KEY] === '1' && session.whenOn[SUBAGENT_ONLY_KEY] !== '1',
  }
}

export const registerStatusBand = (on: On): void => {
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

  // Once the session has been on, the prompt hint row carries the state as a plain tail, so it
  // stays visible after the band hides and flips as soon as a toggle succeeds.
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    const routing = await readRoutingState($)
    if (!routing) return next(e)
    const state = `smart router ${routing.enabled ? 'ON' : 'OFF'}`
    const tail = e.props.tail ? `${e.props.tail} · ${state}` : state
    return next({ ...e, props: { ...e.props, tail } })
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const theirs = await next(e)
    if (!band.isVisible) return theirs
    const routing = await readRoutingState($)
    if (!routing) return theirs
    const { Box, Text } = $.ui.resolve(e)
    const { enabled, firstPrompt } = routing

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
