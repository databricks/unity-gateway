import type { On, PluginOptions } from 'claude-code'

type RoutingRecord = { task: string; model: string; rationale: string; seconds?: number }

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
    children: [Text({ bold: true, children: [record.task] }), ...selection($, e, record, '↳ ', 2)],
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
    if (!command.length) return next(e)
    const { tool, tool_use_id, agentId, ...input } = e
    let route
    let spinner: { cancel: () => void } | undefined
    if (tool_use_id) {
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
    try {
      const session_id = await $.session.id()
      const result = await $.process.run(command, {
        stdin: JSON.stringify({ session_id, tool_use_id, tool_name: tool, tool_input: input }),
        timeoutMs: 35000,
      })
      if (result.exitCode !== 0) throw new Error('Router command failed')
      // An empty response means routing is disabled or no model could be selected.
      route = result.stdout.trim() ? JSON.parse(result.stdout) : undefined
      if (route !== undefined && (
          typeof route?.model !== 'string' || !route.model ||
          typeof route.rationale !== 'string' ||
          typeof route.updatedInput?.subagent_type !== 'string' || !route.updatedInput.subagent_type)) {
        throw new Error('Invalid routing response')
      }
    } catch {
      route = undefined
      $.ui.log('Smart Router could not route this subagent; using its original settings.')
    } finally {
      spinner?.cancel()
    }
    const seconds = tool_use_id ? await secondsSince($, pending.get(tool_use_id)) : undefined
    if (tool_use_id) {
      pending.delete(tool_use_id)
      if (route) {
        records.set(tool_use_id, {
          task: input.description || input.subagent_type || tool,
          model: shortModel(route.model),
          rationale: route.rationale,
          seconds,
        })
      }
      $.ui.invalidate('ui.render')
    }
    if (!route) return next(e)
    // Preserve Claude's permission checks and every other mod's tool handlers.
    return next({ ...route.updatedInput, tool, tool_use_id, agentId })
  })

  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const original = await next(e)
    const record = records.get(e.requestId)
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
    // Claude's row can name only the agent type, so the routed model is always shown,
    // with the reason aligned under "Selected Model" after the arrow.
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
}
