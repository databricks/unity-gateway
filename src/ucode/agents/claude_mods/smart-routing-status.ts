import type { EngineInterface, On } from 'claude-code'
import { SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY } from './constants.ts'
import { isRoutingEnabled } from './routing-state.ts'

const BANNER = 'unity gateway smart router'

// The band shows once the startup check finishes and after each toggle, and hides once the
// next prompt's turn completes. A prompt submitted while it shows arms the hide for that turn.
const band = { isVisible: true, isHideArmed: false }
let validationWatchStarted = false
// Claude draws before session.start, so the band starts held until the startup check settles;
// an unsupported menu turns routing off and must never flash ON.
const validation = { isPending: true }
const VALIDATION_POLL_MS = 100
// Longer than the Python check's 4 s HTTP timeout, so an unsupported result always lands first.
const VALIDATION_WAIT_MS = 5000

export function showStatusBand(): void {
  band.isVisible = true
  band.isHideArmed = false
}

// Hides the band at the end of an armed turn; true when the band changed and needs a redraw.
// The register module owns the one turn.complete handler, which the routing cards share.
export function endStatusBandTurn(): boolean {
  const changed = band.isHideArmed
  if (band.isHideArmed) band.isVisible = false
  band.isHideArmed = false
  return changed
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
    enabled: isRoutingEnabled({ [SMART_ROUTING_KEY]: full, [SUBAGENT_ONLY_KEY]: subagentOnly }),
    firstPrompt: full === '1' && subagentOnly !== '1',
  }
}

export async function watchStartupValidation(
  $: EngineInterface,
  disableRouting: () => Promise<void>,
): Promise<void> {
  if (validationWatchStarted) return
  validationWatchStarted = true
  try {
    const controls = await $.env.get('UCODE_SESSION_ENV_FILE')
    if (typeof controls !== 'string' || !controls.endsWith('.json')) {
      validation.isPending = false
      $.ui.invalidate('ui.render')
      return
    }
    let complete = false
    let waited = 0
    let poll: { cancel: () => void } | undefined
    const finish = (): void => {
      complete = true
      validation.isPending = false
      poll?.cancel()
      $.ui.invalidate('ui.render')
    }
    const check = async (): Promise<void> => {
      if (complete) return
      let result: any
      try {
        result = JSON.parse(await $.fs.read(controls.slice(0, -5) + '.validation.json'))
      } catch {}
      if (complete) return
      if (!result || !('routing_disabled' in result)) {
        if (waited >= VALIDATION_WAIT_MS) finish()
        return
      }
      complete = true
      if (result.routing_disabled === true) {
        band.isVisible = false
        try {
          await disableRouting()
        } catch {}
      }
      finish()
    }
    await check()
    if (!complete) {
      poll = $.clock.every(VALIDATION_POLL_MS, () => {
        waited += VALIDATION_POLL_MS
        return check()
      })
    }
  } catch {
    validation.isPending = false
  }
}

export const registerStatusBand = (on: On): void => {
  let running = 0
  let refresh: { cancel: () => void } | undefined
  on('prompt.submit', ($, e, next) => {
    if (band.isVisible) band.isHideArmed = true
    return next(e)
  })

  // The prompt hint row always carries the state as a plain tail, so it stays visible after
  // the band hides and flips as soon as a toggle succeeds.
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    const { enabled } = await readRoutingState($)
    running = (await $.agent.list()).filter((agent) => agent.status === 'running').length
    // Background agents can change state without changing Claude's cached hint props.
    if (!refresh) refresh = $.clock.every(1000, async () => {
      const count = (await $.agent.list()).filter((agent) => agent.status === 'running').length
      if (count !== running) $.ui.invalidate('ui.render')
    })
    const agents = running ? ` · ${running} subagent${running === 1 ? '' : 's'} running` : ''
    const state = validation.isPending ? '' : `smart router ${enabled ? 'ON' : 'OFF'}${agents}`
    const tail = state && e.props.tail ? `${e.props.tail} · ${state}` : state || e.props.tail
    return next({ ...e, props: { ...e.props, tail } })
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const theirs = await next(e)
    const isBandShown = band.isVisible && !validation.isPending
    if (!isBandShown) return theirs
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
    const children = [line]
    if (theirs) children.push(theirs)
    return Box({ flexDirection: 'column', children })
  })
}
