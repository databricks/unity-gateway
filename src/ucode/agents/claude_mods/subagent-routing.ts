import type { On } from 'claude-code'

// The subagent-routing concern of ug's smart-routing UI mod (composed by
// register.ts). Under ENABLE_CLAUDE_CODE_MODS, ug suppresses its plain-text
// subagent routing notice and this stacks a compact block under each Agent row:
//
//   ⏺ Agent(map the token refresh flow)
//     ↳ explorer  claude-sonnet-5
//       Reason: A focused task with an explicit target…
//
// The routed model comes off the Agent row's result (resolvedModel); the subagent
// name and reason come from the router rationale and the tool input, which
// ug's Python subagent hook writes (keyed by task) into a per-session file next to
// UCODE_SESSION_ENV_FILE.

type RoutingRecord = { model?: string; rationale?: string }

// Reason text as the plain-text box showed it; only the trailing "Request: <task>"
// echo is dropped, since the task is already named on the Agent row.
function parseReason(text: string | undefined): string {
  if (!text) return ''
  return text.replace(/[.\s]*Request:.*$/is, '').trim()
}

// Drop the provider prefix so the model reads compactly (system.ai.X -> X).
const shortModel = (model: string): string => model.replace(/^system\.ai\./, '').replace(/^databricks-/, '')

const taskKey = (input: any): string =>
  String(input?.prompt ?? input?.description ?? input?.subagent_type ?? '')

async function readRouting($: any): Promise<Record<string, RoutingRecord>> {
  let file: string | undefined
  try {
    file = await $.env.get('UCODE_SESSION_ENV_FILE')
  } catch {}
  if (!file) return {}
  const dir = file.slice(0, Math.max(file.lastIndexOf('/'), file.lastIndexOf('\\')))
  try {
    const parsed = JSON.parse(await $.fs.read(`${dir}/mod-routing.json`))
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

export const registerSubagentRouting = (on: On): void => {
  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const original = await next(e)
    const props = e.props as any
    if (props?.tool !== 'Agent' && props?.tool !== 'Task') return original
    const enabled = await $.env.get('ENABLE_CLAUDE_CODE_MODS')
    if (enabled?.trim().toLowerCase() !== '1' && enabled?.trim().toLowerCase() !== 'true') return original

    const routing = await readRouting($)
    const record = routing[taskKey(props.input)] ?? {}
    const model: string | undefined = props.output?.resolvedModel ?? record.model
    const reason = parseReason(record.rationale)
    const name = String(props.input?.subagent_type ?? '').trim()

    const { Box, Text } = $.ui.resolve(e)
    // Until the subagent has a routed model, show "routing…" rather than a blank
    // row — routing can take a moment and this is the only live progress signal.
    const header = Box({
      flexDirection: 'row',
      children: [
        Text({ dimColor: true, children: ['  ↳ '] }),
        ...(name ? [Text({ color: 'primary', bold: true, children: [`${name}  `] })] : []),
        Text({ color: 'primary', children: [model ? shortModel(model) : 'routing…'] }),
      ],
    })
    // Stack the Reason on its own line below so neither the model nor the
    // rationale gets squeezed/truncated by a shared row.
    const children = reason
      ? [original, header, Text({ dimColor: true, children: [`      Reason: ${reason}`] })]
      : [original, header]
    return Box({ flexDirection: 'column', children })
  })
}
