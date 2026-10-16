import type { On, PluginOptions } from 'claude-code'

type RoutingRecord = { model: string; rationale: string; seconds?: number }

const shortModel = (model: string): string => model.replace(/^system\.ai\./, '').replace(/^databricks-/, '')
const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
const SPINNER_INTERVAL_MS = 100

// The timer is cosmetic: without a clock it is omitted rather than blocking routing.
const now = async ($: any): Promise<number | undefined> => {
  try {
    return await $.clock.now()
  } catch {
    return undefined
  }
}

export const registerSubagentRouting = (on: On, options: PluginOptions): void => {
  const records = new Map<string, RoutingRecord>()
  // Calls waiting on the router, by when routing started, shown with a spinner and timer.
  const pending = new Map<string, number | undefined>()
  let frame = 0

  on('tool.call', { tool: ['Agent', 'Task'] }, async ($, e, next) => {
    const command = options.routeCommand as readonly string[]
    if (!command.length) return next(e)
    const { tool, tool_use_id, agentId, ...input } = e
    let route
    let spinner: { cancel: () => void } | undefined
    if (tool_use_id) {
      pending.set(tool_use_id, await now($))
      // Mods have no global timers; $.clock ticks the spinner while the router answers.
      try {
        spinner = $.clock.every(SPINNER_INTERVAL_MS, () => {
          frame = (frame + 1) % SPINNER.length
          $.ui.invalidate('ui.render')
        })
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
      const startedAt = pending.get(tool_use_id)
      pending.delete(tool_use_id)
      const finishedAt = startedAt === undefined ? undefined : await now($)
      if (route) {
        records.set(tool_use_id, {
          model: route.model,
          rationale: route.rationale,
          seconds: finishedAt === undefined || startedAt === undefined
            ? undefined
            : Math.max(0, finishedAt - startedAt) / 1000,
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
      const startedAt = pending.get(e.requestId)
      const current = startedAt === undefined ? undefined : await now($)
      const elapsed = current === undefined || startedAt === undefined
        ? ''
        : ` ${Math.max(0, Math.floor((current - startedAt) / 1000))}s`
      return Box({ flexDirection: 'column', children: [original, Box({
        flexDirection: 'row',
        children: [
          Text({ dimColor: true, children: ['  ↳ '] }),
          Text({ color: 'green', children: [`${SPINNER[frame]} `] }),
          Text({ dimColor: true, children: [`Selecting a model…${elapsed}`] }),
        ],
      })] })
    }
    // Claude's row can name only the agent type, so the routed model is always shown.
    const children = [original, Box({
      flexDirection: 'row',
      children: [
        // Labels recede so the routed model stands out.
        Text({ dimColor: true, children: ['  ↳ Selected Model: '] }),
        // The same green as the status band's ON pill.
        Text({ color: 'green', bold: true, children: [shortModel(record.model)] }),
        // How long routing took; omitted when no clock was available.
        ...(record.seconds === undefined
          ? []
          : [Text({ dimColor: true, children: [` (routed in ${record.seconds.toFixed(1)}s)`] })]),
      ],
    })]
    // Align the reason, including its wrapped lines, under "Selected Model" after the arrow.
    if (record.rationale) {
      children.push(Box({
        paddingLeft: 4,
        children: [Text({ children: [Text({ dimColor: true, children: ['Reason: '] }), record.rationale] })],
      }))
    }
    return Box({ flexDirection: 'column', children })
  })
}
