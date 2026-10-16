import type { EngineInterface, On, RenderElement } from 'claude-code'
import { SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY } from './constants.ts'

const BANNER = 'unity gateway smart router'
const DIRECT = 'Main agent is not spawning subagents and is working on the task directly.'
const SPAWNING = 'Main agent is spawning subagents.'

// The band shows at launch and after each toggle, and hides once the next prompt's turn
// completes. A prompt submitted while it shows arms the hide for that prompt's turn.
const band = { isVisible: true, isHideArmed: false }

export function showStatusBand(): void {
  band.isVisible = true
  band.isHideArmed = false
}

type RoutingState = { enabled: boolean; firstPrompt: boolean; orchestrator: boolean }

// Claude's capability scanner requires literal names in $.env.get calls.
async function readRoutingState($: EngineInterface): Promise<RoutingState> {
  let full: unknown
  let subagentOnly: unknown
  let orchestrator: unknown
  let launchMode: unknown
  let file: unknown
  let hasControls = false
  try {
    full = await $.env.get('ENABLE_SMART_ROUTING_V2')
  } catch {}
  try {
    subagentOnly = await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY')
  } catch {}
  try {
    file = await $.env.get('UCODE_SESSION_ENV_FILE')
  } catch {}
  try {
    orchestrator = await $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR')
    launchMode = await $.env.get('ISAAC_LAUNCH_MODE')
  } catch {}
  if (typeof file === 'string' && file) {
    try {
      const overrides = JSON.parse(await $.fs.read(file))
      if (overrides && typeof overrides === 'object' && !Array.isArray(overrides)) {
        hasControls = true
        if (SMART_ROUTING_KEY in overrides) full = overrides[SMART_ROUTING_KEY]
        if (SUBAGENT_ONLY_KEY in overrides) subagentOnly = overrides[SUBAGENT_ONLY_KEY]
        if ('ENABLE_SMART_ROUTER_ORCHESTRATOR' in overrides) {
          orchestrator = overrides.ENABLE_SMART_ROUTER_ORCHESTRATOR
        }
      }
    } catch {}
  }
  return {
    enabled: full === '1' || subagentOnly === '1',
    firstPrompt: full === '1' && subagentOnly !== '1',
    orchestrator: hasControls && String(launchMode ?? '').trim().toLowerCase() !== 'omni' && orchestrator === '1' &&
      (full === '1' || subagentOnly === '1'),
  }
}

export const registerStatusBand = (on: On, optedIn: boolean): void => {
  let tool = ''
  let interactive = false
  let turn: string | undefined
  let pendingRequests = 0
  let choice: typeof DIRECT | typeof SPAWNING | undefined

  on('session.start', { isInteractive: true }, async ($, e, next) => {
    const result = await next(e)
    interactive = e.isInteractive
    turn = undefined
    pendingRequests = 0
    choice = undefined
    if (interactive && optedIn) {
      tool = (await $.tool.register({
        name: 'working_directly',
        description: 'Main agent only: report that this request will be handled directly without spawning subagents. Updates the status indicator only.',
        inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      })).tool
    }
    $.ui.invalidate('ui.render')
    return result
  })

  on('prompt.submit', async ($, e, next) => {
    if (band.isVisible) band.isHideArmed = true
    if (e.origin.kind === 'task-notification' || e.origin.kind === 'peer-send-message') return next(e)
    pendingRequests += 1
    try {
      const prompt = tool && (await readRoutingState($)).orchestrator
        ? { ...e, context: [...(e.context ?? []),
            `Follow the existing orchestration workflow. After deciding to work directly without spawning subagents, call ${tool} with {} before doing the task. If spawning subagents, use the native Agent/Task tool as usual; its call updates the same indicator automatically. This reports your choice only; keep the existing tool permissions and spawning policy. Only the main agent reports this choice.`,
          ] }
        : e
      const result = await next(prompt)
      if ('drop' in result) pendingRequests = Math.max(0, pendingRequests - 1)
      return result
    } catch (error) {
      pendingRequests = Math.max(0, pendingRequests - 1)
      throw error
    }
  })

  on('turn.start', async ($, e, next) => {
    if (turn !== e.turnId) {
      turn = e.turnId
      if (pendingRequests > 0) {
        pendingRequests -= 1
        choice = undefined
      }
      $.ui.invalidate('ui.render')
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    if (e.tool !== tool && e.tool !== 'Agent' && e.tool !== 'Task') return next(e)
    if (e.agentId) return e.tool === tool ? { deny: 'Only the main agent can report its choice.' } : next(e)
    const request = turn
    const enabled = interactive && optedIn && (await readRoutingState($)).orchestrator
    if (request && request === turn && enabled) {
      if (e.tool !== tool) choice = SPAWNING
      else if (choice !== SPAWNING) choice = DIRECT
      $.ui.invalidate('ui.render')
    }
    return e.tool === tool ? { result: { reported: Boolean(request && request === turn && enabled) } } : next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId && e.turnId === turn) turn = undefined
    if (band.isHideArmed) {
      band.isVisible = false
      band.isHideArmed = false
      $.ui.invalidate('ui.render')
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    turn = undefined
    pendingRequests = 0
    choice = undefined
    $.ui.invalidate('ui.render')
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
    const { enabled, firstPrompt, orchestrator } = await readRoutingState($)
    const status = interactive && optedIn && orchestrator ? choice : undefined
    if (!band.isVisible && !status) return theirs
    const { Box, Text } = $.ui.resolve(e)

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
    const children: RenderElement[] = band.isVisible ? [line] : []
    if (status) children.push(Text({ children: [status] }))
    if (theirs) children.push(theirs)
    return Box({ flexDirection: 'column', children })
  })
}
