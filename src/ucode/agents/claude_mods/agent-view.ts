import type { On } from 'claude-code'

// The host's native view switch: the main view shows one subagent's own
// transcript and the composer's next Enter is addressed to that subagent.
// Engines before 2.1.319 refuse the ui.view event at load, so the generated
// plugin ships a stub of this file for them (see _write_routed_claude_plugin);
// NATIVE_VIEW tells the mod and its tests which form arrived.
export const NATIVE_VIEW = true

// The tile press hands the switch to the host's own ui.press hook below, so
// the tile's handler beneath the chain needs to know the press was taken.
let switched: string | undefined

export function pressSwitchedView(id: string): boolean {
  return switched === id
}

export function registerAgentView(on: On): void {
  on('ui.press', async ($, e, next) => {
    if (e.plugin !== $.plugin.name) return next(e)
    const id = e.element.startsWith('agent:') ? e.element.slice('agent:'.length) : undefined
    if (id) {
      const result = await $.ui.view({ agentId: id })
      switched = result && !result.deny ? id : undefined
      return next(e)
    }
    if (e.element === 'back-to-main') {
      await $.ui.view({})
      switched = undefined
    }
    return next(e)
  })
}
