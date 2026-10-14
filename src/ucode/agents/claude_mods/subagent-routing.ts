import type { On, PluginOptions } from 'claude-code'

type RoutingRecord = { model: string; rationale: string; name: string }

const shortModel = (model: string): string => model.replace(/^system\.ai\./, '').replace(/^databricks-/, '')

export const registerSubagentRouting = (on: On, options: PluginOptions): void => {
  const records = new Map<string, RoutingRecord>()

  on('tool.call', { tool: ['Agent', 'Task'] }, async ($, e, next) => {
    const command = options.routeCommand as readonly string[]
    if (!command.length) return next(e)
    const { tool, tool_use_id, agentId, ...input } = e
    let route
    try {
      const session_id = await $.session.id()
      const result = await $.process.run(command, {
        stdin: JSON.stringify({ session_id, tool_use_id, tool_name: tool, tool_input: input }),
        timeoutMs: 35000,
      })
      if (result.exitCode !== 0) throw new Error('Router command failed')
      // An empty response means routing is disabled or no model could be selected.
      if (result.stdout.trim()) {
        route = JSON.parse(result.stdout)
        if (typeof route?.model !== 'string' || !route.model ||
            typeof route.rationale !== 'string' ||
            typeof route.updatedInput?.subagent_type !== 'string' || !route.updatedInput.subagent_type) {
          throw new Error('Invalid routing response')
        }
      }
    } catch {
      route = undefined
      $.ui.log('Smart Router could not route this subagent; using its original settings.')
    }
    if (!route) return next(e)
    if (tool_use_id) {
      records.set(tool_use_id, {
        model: route.model,
        rationale: route.rationale,
        name: typeof input.subagent_type === 'string' ? input.subagent_type : '',
      })
      $.ui.invalidate('ui.render')
    }
    // Preserve Claude's permission checks and every other mod's tool handlers.
    return next({ ...route.updatedInput, tool, tool_use_id, agentId })
  })

  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const original = await next(e)
    const record = records.get(e.requestId)
    if (!record) return original

    const { Box, Text } = $.ui.resolve(e)
    const header = Box({
      flexDirection: 'row',
      children: [
        Text({ dimColor: true, children: ['  ↳ '] }),
        ...(record.name ? [Text({ color: 'primary', bold: true, children: [`${record.name}  `] })] : []),
        Text({ color: 'primary', children: [shortModel(record.model)] }),
      ],
    })
    const children = record.rationale
      ? [original, header, Text({ dimColor: true, children: [`      Reason: ${record.rationale}`] })]
      : [original, header]
    return Box({ flexDirection: 'column', children })
  })
}
