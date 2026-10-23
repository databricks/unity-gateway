import type { On, PluginOptions } from 'claude-code'

type RoutingRecord = { model: string; rationale: string; seconds?: number }

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

export const registerSubagentRouting = (on: On, options: PluginOptions): void => {
  const records = new Map<string, RoutingRecord>()
  // Calls waiting on the router, by when routing started, shown with a spinner and timer.
  const pending = new Map<string, number | undefined>()

  on('tool.call', { tool: ['Agent', 'Task'] }, async ($, e, next) => {
    const command = options.routeCommand as readonly string[]
    if (!command.length) return next(e)
    const { tool, tool_use_id, agentId, ...input } = e
    let route
    let spinner: { cancel: () => void } | undefined
    if (tool_use_id) {
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
    if (tool_use_id) {
      const seconds = await secondsSince($, pending.get(tool_use_id))
      pending.delete(tool_use_id)
      if (route) records.set(tool_use_id, { model: route.model, rationale: route.rationale, seconds })
      $.ui.invalidate('ui.render')
    }
    if (!route) return next(e)
    // Preserve Claude's permission checks and every other mod's tool handlers.
    return next({ ...route.updatedInput, tool, tool_use_id, agentId })
  })

  on('ui.render', { component: ['ToolUse', 'ToolGroup'] }, async ($, e, next) => {
    const original = await next(e)
    const grouped = e.component === 'ToolGroup'
    if (grouped && e.props.isExpanded) return original
    const calls = grouped ? e.props.calls : [e.props]

    const { Box, Text } = $.ui.resolve(e)
    const children = [original]
    for (const call of calls) {
      const record = records.get(call.tool_use_id)
      if (!record && !pending.has(call.tool_use_id)) continue
      if (grouped) {
        children.push(Text({ bold: true, children: [`  ${call.input.description || call.input.subagent_type || call.tool}`] }))
      }
      if (!record) {
        const glyph = SPINNER[Math.floor(((await now($)) ?? 0) / SPINNER_INTERVAL_MS) % SPINNER.length]
        const seconds = await secondsSince($, pending.get(call.tool_use_id))
        const elapsed = seconds === undefined ? '' : ` ${Math.floor(seconds)}s`
        children.push(Box({
          flexDirection: 'row',
          children: [
            Text({ dimColor: true, children: ['  ↳ '] }),
            Text({ color: 'green', children: [`${glyph} `] }),
            Text({ dimColor: true, children: [`Selecting a model…${elapsed}`] }),
          ],
        }))
        continue
      }
      children.push(Box({
        flexDirection: 'row',
        children: [
          Text({ dimColor: true, children: ['  ↳ Selected Model: '] }),
          Text({ color: 'green', bold: true, children: [shortModel(record.model)] }),
          ...(record.seconds === undefined
            ? []
            : [Text({ dimColor: true, children: [` (routed in ${record.seconds.toFixed(1)}s)`] })]),
        ],
      }))
      if (record.rationale) {
        children.push(Box({
          paddingLeft: 4,
          children: [Text({ children: [Text({ dimColor: true, children: ['Reason: '] }), record.rationale] })],
        }))
      }
    }
    return children.length === 1 ? original : Box({ flexDirection: 'column', children })
  })
}
