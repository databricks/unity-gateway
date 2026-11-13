import type { On, PluginOptions } from 'claude-code'

const ROUTER_TIMEOUT_MS = 35000

type RoutingRecord = {
  name: string; task: string; model: string; rationale: string; seconds?: number
  spawned?: { agentId: string; model?: string; background: boolean }
}

const routingDiagnostics = (stderr: string): string[] => {
  const records: string[] = []
  for (const line of stderr.slice(-32768).split('\n')
    .filter(line => line.startsWith('[ug-routing] ')).slice(-8)) {
    if (line.length > 4096) continue
    try {
      const record: unknown = JSON.parse(line.slice('[ug-routing] '.length))
      if (!record || typeof record !== 'object' || !('v' in record) || record.v !== 1
          || !('event' in record) || typeof record.event !== 'string'
          || !['request_start', 'request_finish', 'hook_start', 'hook_finish'].includes(record.event)) continue
      const safe: Record<string, string | number> = { v: 1, event: record.event }
      for (const [key, value] of Object.entries(record)) {
        if (['phase', 'outcome', 'error_type'].includes(key)
            && typeof value === 'string' && /^[A-Za-z_]{1,64}$/.test(value)) safe[key] = value
        if (['client_request_id', 'server_request_id', 'hook_id', 'tool_use_id', 'router_name'].includes(key)
            && typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value)) safe[key] = value
        if (['at', 'elapsed_ms', 'timeout_s', 'http_status', 'open_response_ms',
          'read_body_ms', 'decode_response_ms', 'validate_selection_ms', 'route_option_count'].includes(key)
            && typeof value === 'number' && Number.isFinite(value) && value >= 0) safe[key] = value
      }
      records.push('[ug-routing] ' + JSON.stringify(safe))
    } catch {}
  }
  return records
}

const safeText = (value: unknown, limit: number): string | undefined => {
  if (typeof value !== 'string') return undefined
  const clean = value
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, limit)
  return clean || undefined
}

const canonicalModel = (model: string): string => {
  const original = model.replace(/^anthropic-aigw-[0-9a-fA-F]{8}-/, '')
  if (original.startsWith('system.ai.')) return original.slice('system.ai.'.length).replace(/\[1m\]$/, '')
  return original.split('.').length === 3 ? original : original.replace(/\[1m\]$/, '')
}

const children = new Map<string, RoutingRecord>()
export const routedModels = {
  started(record: RoutingRecord) { children.set(record.spawned!.agentId, record) },
  model(agentId?: string) {
    const child = agentId ? children.get(agentId) : undefined
    const resolved = child?.spawned?.model
    return child && resolved && canonicalModel(resolved) === canonicalModel(child.model)
      ? resolved : child?.model
  },
  reset() { children.clear() },
}

const ANIMALS = [
  'Otter', 'Panda', 'Lynx', 'Fox', 'Badger', 'Beaver', 'Bison', 'Bobcat', 'Camel', 'Caribou',
  'Cheetah', 'Chipmunk', 'Cougar', 'Coyote', 'Deer', 'Dingo', 'Dolphin', 'Donkey', 'Elephant', 'Elk',
  'Ferret', 'Gazelle', 'Giraffe', 'Goat', 'Gopher', 'Gorilla', 'Hamster', 'Hare', 'Hedgehog', 'Hippo',
  'Horse', 'Hyena', 'Ibex', 'Impala', 'Jaguar', 'Kangaroo', 'Koala', 'Lemur', 'Leopard', 'Lion',
  'Llama', 'Marmot', 'Meerkat', 'Mink', 'Mole', 'Mongoose', 'Moose', 'Mouse', 'Narwhal', 'Ocelot',
  'Okapi', 'Orca', 'Pangolin', 'Platypus', 'Porcupine', 'Possum', 'Puma', 'Quokka', 'Rabbit', 'Raccoon',
  'Reindeer', 'Rhino', 'Seal', 'Sloth', 'Squirrel', 'Tapir', 'Tiger', 'Vole', 'Walrus', 'Warthog',
  'Weasel', 'Whale', 'Wolf', 'Wombat', 'Yak', 'Zebra', 'Albatross', 'Crane', 'Crow', 'Dove',
  'Eagle', 'Falcon', 'Finch', 'Flamingo', 'Heron', 'Ibis', 'Jay', 'Kestrel', 'Kiwi', 'Lark',
  'Magpie', 'Osprey', 'Owl', 'Parrot', 'Pelican', 'Penguin', 'Puffin', 'Raven', 'Robin', 'Swan',
]

const shortModel = (model: string): string => model.replace(/^system\.ai\./, '').replace(/^databricks-/, '')
// Claude's own spinner glyphs, played forwards then back as Claude does.
const SPINNER_GLYPHS = ['·', '✢', '✳', '✶', '✻', '✽']
const SPINNER = [...SPINNER_GLYPHS, ...SPINNER_GLYPHS.slice(1, -1).reverse()]
const SPINNER_INTERVAL_MS = 100

// The timer is cosmetic: without a clock it is omitted rather than blocking routing.
const now = async ($: any): Promise<number | undefined> => {
  try {
    return await $.clock.now()
  } catch {
    return undefined
  }
}
const secondsSince = async ($: any, start: number | undefined): Promise<number | undefined> => {
  const current = start === undefined ? undefined : await now($)
  return current === undefined || start === undefined ? undefined : Math.max(0, current - start) / 1000
}

// The selected model with its routing time, then the reason. Labels recede so the routed
// model, in the status band's ON green, stands out; the time is omitted without a clock.
const selection = ($: any, e: any, record: RoutingRecord, arrow: string, reasonPadding: number) => {
  const { Box, Text } = $.ui.resolve(e)
  const lines = [Box({
    flexDirection: 'row',
    children: [
      Text({ dimColor: true, children: [`${arrow}Selected Model: `] }),
      Text({ color: 'green', bold: true, children: [shortModel(record.model)] }),
      ...(record.seconds === undefined ? [] : [
        Text({ dimColor: true, children: [` (routed in ${record.seconds.toFixed(1)}s)`] }),
      ]),
    ],
  })]
  if (record.spawned?.model && record.spawned.model !== record.model) {
    lines.push(Box({ paddingLeft: reasonPadding,
      children: [Text({ children: [`Launched Model: ${safeText(record.spawned.model, 240)}`] })],
    }))
  }
  if (record.rationale) {
    lines.push(Box({
      paddingLeft: reasonPadding,
      children: [Text({ children: [Text({ dimColor: true, children: ['Reason: '] }), record.rationale] })],
    }))
  }
  return lines
}

// A grouped call's decision: its task, then the selection.
const card = ($: any, e: any, record: RoutingRecord) => {
  const { Box, Text } = $.ui.resolve(e)
  return Box({
    flexDirection: 'column',
    paddingLeft: 2,
    marginBottom: 1,
    children: [Text({ bold: true, children: [record.name, ' — ', record.task] }), ...selection($, e, record, '↳ ', 2)],
  })
}

// Grouped calls not yet drawn, in call order. They live outside registration so the
// register module's one turn.complete handler can drop them.
const cards = { unplaced: [] as string[] }

// An aborted turn draws no reply, so its calls must not reach a later turn. A turn that
// merely ends, as when the reply waits on a background agent, keeps them.
export function endSubagentRoutingTurn(aborted: boolean): void {
  if (aborted) cards.unplaced = []
}

export const registerSubagentRouting = (on: On, options: PluginOptions): void => {
  routedModels.reset()
  const names = new Map<string, string>()
  const unusedNames = [...ANIMALS]
  // Calls waiting on the router, by when routing started, shown with a spinner and timer.
  const pending = new Map<string, number | undefined>()
  // Claude folds parallel Agent rows into its own block, which no hook reaches, so a call
  // that was routing alongside another is drawn as a card above the parent's next reply.
  // A lone call keeps its decision under its own row.
  const grouped = new Set<string>()
  const records = new Map<string, RoutingRecord>()
  // The reply each batch is drawn above.
  const anchors = new Map<string, string[]>()
  // Replies already drawn before a batch was routed are never its anchor.
  const seen = new Set<string>()

  on('tool.call', { tool: ['Agent', 'Task'] }, async ($, e, next) => {
    const command = options.routeCommand as readonly string[]
    if (!command.length || e.subagent_type === 'fork' || e.resume) return next(e)
    const { tool, tool_use_id, agentId: _agentId, ...input } = e
    let route
    let spinner: { cancel: () => void } | undefined
    if (tool_use_id) {
      if (!names.has(tool_use_id)) {
        if (!unusedNames.length) unusedNames.push(...ANIMALS)
        const animal = unusedNames.splice(Math.floor(Math.random() * unusedNames.length), 1)[0]
        const cycle = Math.floor(names.size / ANIMALS.length)
        names.set(tool_use_id, cycle ? `${animal} ${cycle + 1}` : animal)
      }
      // Parallel calls start together, so one still routing means this call is not alone.
      if (pending.size) {
        for (const id of pending.keys()) {
          if (!grouped.has(id)) {
            grouped.add(id)
            cards.unplaced.push(id)
          }
        }
        grouped.add(tool_use_id)
        cards.unplaced.push(tool_use_id)
      }
      pending.set(tool_use_id, undefined)
      pending.set(tool_use_id, await now($))
      // Mods have no global timers; $.clock redraws the spinner while the router answers.
      // The frame comes from the clock, so parallel calls do not speed it up.
      try {
        spinner = $.clock.every(SPINNER_INTERVAL_MS, () => $.ui.invalidate('ui.render'))
      } catch {}
      $.ui.invalidate('ui.render')
    }
    const hookStarted = await now($)
    let hookOutcome = 'process_error'
    const diagnosticToolId = typeof tool_use_id === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(tool_use_id)
      ? tool_use_id : undefined
    const logDiagnostic = (text: string) => {
      try { $.ui.log(text, { to: 'debug' }) } catch {}
    }
    logDiagnostic('[ug-routing] ' + JSON.stringify({
      v: 1, event: 'command_start', tool_use_id: diagnosticToolId, timeout_ms: ROUTER_TIMEOUT_MS,
    }))
    try {
      const session_id = await $.session.id()
      const result = await $.process.run(command, {
        stdin: JSON.stringify({ session_id, tool_use_id, tool_name: tool, tool_input: input }),
        timeoutMs: ROUTER_TIMEOUT_MS,
      })
      for (const diagnostic of routingDiagnostics(result.stderr)) logDiagnostic(diagnostic)
      hookOutcome = 'nonzero_exit'
      if (result.exitCode !== 0) throw new Error('Router command failed')
      // An empty response means routing is disabled or no model could be selected.
      hookOutcome = 'invalid_output'
      route = result.stdout.trim() ? JSON.parse(result.stdout) : undefined
      if (route !== undefined && (
          typeof route?.model !== 'string' || !route.model ||
          typeof route.rationale !== 'string')) {
        throw new Error('Invalid routing response')
      }
      hookOutcome = route === undefined ? 'empty_output' : 'resolved'
    } catch {
      route = undefined
      $.ui.log('Smart Router could not route this subagent; using its original settings.')
    } finally {
      spinner?.cancel()
      const hookSeconds = await secondsSince($, hookStarted)
      logDiagnostic('[ug-routing] ' + JSON.stringify({
        v: 1, event: 'command_finish', outcome: hookOutcome,
        tool_use_id: diagnosticToolId,
        elapsed_ms: hookSeconds === undefined ? undefined : Math.round(hookSeconds * 1000),
        timeout_ms: ROUTER_TIMEOUT_MS,
      }))
    }
    const seconds = tool_use_id ? await secondsSince($, pending.get(tool_use_id)) : undefined
    if (tool_use_id) {
      pending.delete(tool_use_id)
      if (route) {
        records.set(tool_use_id, {
          name: names.get(tool_use_id)!,
          task: input.description || input.subagent_type || tool,
          model: route.model,
          rationale: route.rationale,
          seconds,
        })
      }
      $.ui.invalidate('ui.render')
    }
    // Auto mode reviewed this input; model identity belongs in the result, not its arguments.
    const result = await next(e)
    const record = tool_use_id ? records.get(tool_use_id) : undefined
    if (result.deny !== undefined || result.isError || !record?.spawned) return result
    const identity = JSON.stringify({
      agent_id: record.spawned.agentId,
      selected_model: record.model,
      launched_model: record.spawned.model ?? null,
    })
    return { ...result, context: [...(result.context ?? []),
      `Smart Router launch: ${identity}. Use the launched_model from this receipt when identifying `
      + 'this child. Report only that model in ordinary updates and answers; do not compare it with the requested model unless the user asks about the difference.',
    ] }
  })

  on('agent.spawn', async ($, e, next) => {
    const route = e.resume ? undefined : records.get(e.tool_use_id)
    const name = route?.name
    const result = await next(route ? { ...e, model: route.model } : e)
    if (route && result.agentId && !result.deny) {
      route.spawned = { agentId: result.agentId, model: result.model || undefined, background: e.background }
      routedModels.started(route)
      $.ui.invalidate('ui.render')
    }
    if (name && result.agentId) {
      try {
        // The native bottom bar is outside ui.render; join its task ID to this call.
        await $.fs.write(`${$.plugin.root}/agent-names/agent-${encodeURIComponent(result.agentId)}.json`,
          JSON.stringify({ session_id: await $.session.id(), name }))
      } catch {
        $.ui.log('Could not save the subagent display name; its bottom bar will use the native label.')
      }
    }
    return result
  })

  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const record = records.get(e.requestId)
    const name = names.get(e.requestId)
    const original = await next(name && (record || pending.has(e.requestId)) ? {
      ...e,
      props: { ...e.props, tool: name },
    } : e)
    if (!record && !pending.has(e.requestId)) return original

    const { Box, Text } = $.ui.resolve(e)
    if (!record) {
      const glyph = SPINNER[Math.floor(((await now($)) ?? 0) / SPINNER_INTERVAL_MS) % SPINNER.length]
      const seconds = await secondsSince($, pending.get(e.requestId))
      const elapsed = seconds === undefined ? '' : ` ${Math.floor(seconds)}s`
      return Box({ flexDirection: 'column', children: [original, Box({
        flexDirection: 'row',
        children: [
          Text({ dimColor: true, children: ['  ↳ '] }),
          Text({ color: 'green', children: [`${glyph} `] }),
          Text({ dimColor: true, children: [`Selecting a model…${elapsed}`] }),
        ],
      })] })
    }
    if (grouped.has(e.requestId)) return original
    return Box({ flexDirection: 'column', children: [original, ...selection($, e, record, '  ↳ ', 4)] })
  })

  // Claude's parallel-agent block hides the row spinner, so the turn's own spinner line
  // also says routing is under way, timed from the oldest call still waiting.
  on('ui.render', { component: 'Spinner' }, async ($, e, next) => {
    if (!pending.size) return next(e)
    const starts = [...pending.values()].filter((start) => start !== undefined) as number[]
    const seconds = starts.length ? await secondsSince($, Math.min(...starts)) : undefined
    const elapsed = seconds === undefined ? '' : ` ${Math.floor(seconds)}s`
    const what = pending.size > 1 ? `Selecting models for ${pending.size} agents` : 'Selecting a model'
    return next({ ...e, props: { ...e.props, message: `${what}…${elapsed}`, suffix: '' } })
  })

  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    const original = await next(e)
    if (!seen.has(e.requestId)) {
      seen.add(e.requestId)
      // A call still routing waits for a later reply; one that failed routing is dropped.
      const ready = cards.unplaced.filter((id) => records.has(id))
      cards.unplaced = cards.unplaced.filter((id) => pending.has(id))
      if (ready.length) anchors.set(e.requestId, ready)
    }
    const ids = anchors.get(e.requestId)
    if (!ids) return original
    const { Box } = $.ui.resolve(e)
    return Box({ flexDirection: 'column', children: [...ids.map((id) => card($, e, records.get(id)!)), original] })
  })
  on('session.end', {reason: ['clear', 'resume', 'logout', 'prompt_input_exit', 'other']}, async ($, e, next) => {
    routedModels.reset()
    return next(e)
  })
}
