import type { On } from 'claude-code'

// The host's native view switch. Constraint: engines before 2.1.319 refuse the
// ui.view event at load, so the generated plugin ships a stub of this file for
// them, and NATIVE_VIEW tells the mod and its tests which form arrived.
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
      // A host refusal (or an engine error) is a denied switch: the press still
      // runs beneath the chain, which opens the read-only details fallback.
      let result: { deny?: string } | undefined
      try {
        result = await $.ui.view({ agentId: id })
      } catch {
        result = undefined
      }
      switched = result && !result.deny ? id : undefined
      return next(e)
    }
    if (e.element === 'back-to-main') {
      try {
        await $.ui.view({})
        switched = undefined
      } catch {
        // The host refused the return; the pane keeps marking the child in view.
      }
    }
    return next(e)
  })
}
