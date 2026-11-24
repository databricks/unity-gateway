import type { On } from 'claude-code'
import { autoTarget, type AutoPicker } from './auto-model.ts'
import {
  LOGS_DETAILS_HINT, SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY, UNSUPPORTED_MODEL_LIST_WARNING, UNSUPPORTED_ROUTER_STATE,
  VALIDATION_FAILED_WARNING,
} from './constants.ts'
import { isRoutingEnabled } from './routing-state.ts'

const BANNER = 'unity gateway smart router'

// The band shows once the route validation finishes and after each toggle, and hides once the
// next prompt's turn completes. A prompt submitted while it shows arms the hide for that turn.
const band = { isVisible: true, isHideArmed: false, isUnsupported: false, isValidationFailed: false }
let validationWatchStarted = false
// Claude draws before session.start, so the band starts held until the route validation settles;
// an unsupported model list turns routing off and must never flash ON.
const validation = { isPending: true }

export function skipStartupValidation(): void {
  validationWatchStarted = true
  validation.isPending = false
}

export function showStatusBand(): void {
  band.isVisible = true
  band.isHideArmed = false
  band.isUnsupported = false
  band.isValidationFailed = false
}

// Hides the band at the end of an armed turn; true when the band changed and needs a redraw.
// The register module owns the one turn.complete handler, which the routing cards share.
export function endStatusBandTurn(): boolean {
  // A turn that ends while the route validation is pending keeps the hide armed for the next turn.
  if (!band.isHideArmed || validation.isPending) return false
  band.isVisible = false
  band.isHideArmed = false
  return true
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

export function startStartupValidation(): boolean {
  if (validationWatchStarted) return false
  validationWatchStarted = true
  return true
}

export function settleStartupValidation(): void {
  validation.isPending = false
}

export function warnUnsupportedModelList(): void {
  band.isUnsupported = true
}

export function warnValidationFailed(): void {
  band.isValidationFailed = true
}

function routingWarning(): string | undefined {
  if (band.isValidationFailed) return VALIDATION_FAILED_WARNING
  return band.isUnsupported ? UNSUPPORTED_MODEL_LIST_WARNING : undefined
}

export const registerStatusBand = (on: On, auto?: AutoPicker): void => {
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
    const warning = routingWarning()
    const routerState = enabled ? 'ON' : warning ? UNSUPPORTED_ROUTER_STATE : 'OFF'
    let state = validation.isPending ? '' : `smart router ${routerState}${agents}`
    if (auto && state) {
      // The static Auto row is the default variant's alias; name the session's main model.
      const target = autoTarget(auto, await $.session.model())
      const main = target === undefined ? undefined : auto.models.find((row) => row.model === target)
      if (main) state = `Auto (${main.label}) · ${state}`
    }
    const tail = state && e.props.tail ? `${e.props.tail} · ${state}` : state || e.props.tail
    return next({ ...e, props: { ...e.props, tail } })
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const theirs = await next(e)
    const isBandShown = band.isVisible && !validation.isPending
    if (!isBandShown) return theirs
    const { Box, Text } = $.ui.resolve(e)
    const { enabled, firstPrompt } = await readRoutingState($)

    const warning = routingWarning()
    const pill = enabled
      ? Text({ bold: true, color: 'black', backgroundColor: 'green', children: [' ON '] })
      : Text({ bold: true, color: 'black', backgroundColor: warning ? 'yellow' : 'gray', children: [' OFF '] })
    const trailer = warning
      ? Text({ color: 'yellow', children: [`${warning} ${LOGS_DETAILS_HINT}`] })
      : enabled
      ? Text({
          dimColor: true,
          italic: true,
          children: ['— ' + (firstPrompt ? 'routing first prompt + subagents' : 'routing subagents')],
        })
      : Text({ dimColor: true, children: [auto ? "— Reenable with Auto in '/model'" : "— Reenable with '/smart-router on'"] })

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
