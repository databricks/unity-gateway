import type { On, PluginOptions } from 'claude-code'
import { ROUTED_AGENT_PREFIX } from './constants.ts'

type RoutingRecord = { model: string; rationale: string; name: string; orchestrator: boolean }

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
    }
    if (!route) return next(e)
    if (tool_use_id) {
      // Name the agent that is dispatched, not the type the parent originally requested.
      // UG's own routed definitions are omitted: the model already says what they are.
      const name = route.updatedInput.subagent_type
      records.set(tool_use_id, {
        model: route.model,
        rationale: route.rationale,
        name: name.startsWith(ROUTED_AGENT_PREFIX) ? '' : name,
        orchestrator: route.orchestrator === true,
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
        // The lightning bolt marks the line as Smart Router's, not Claude's own output.
        Text({ children: ['⚡ '] }),
        ...(record.name ? [Text({ color: 'primary', bold: true, children: [`${record.name}  `] })] : []),
        Text({ color: 'primary', children: [shortModel(record.model)] }),
        ...(record.orchestrator ? [Text({ dimColor: true, children: [' [orchestrator on]'] })] : []),
      ],
    })
    const children = record.rationale
      ? [original, header, Text({ dimColor: true, children: [`      Reason: ${record.rationale}`] })]
      : [original, header]
    return Box({ flexDirection: 'column', children })
  })
}
