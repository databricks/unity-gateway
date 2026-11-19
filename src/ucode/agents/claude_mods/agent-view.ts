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

// The host rejects a press whose element a re-render has released; the
// rejection surfaces through this hook's next(), and the host would report the
// hook as skipped for it, so tolerate it where no live handler remains.
function releasedPressHandle(error: unknown): boolean {
  return error instanceof Error &&
    /^ui\.(?:press|input|select)(?:\/ui\.(?:press|input|select))*: no handler is held under handle \d+$/.test(error.message)
}

export function registerAgentView(on: On): void {
  on('ui.press', async ($, e, next) => {
    if (e.plugin !== $.plugin.name) {
      try {
        return await next(e)
      } catch (error) {
        if (!releasedPressHandle(error)) throw error
        return e
      }
    }
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
      if (switched !== undefined) {
        // The switch re-rendered this pane and released the pressed element's
        // handler; the tile's own press no-ops once the switch is taken, so
        // handing the press beneath the chain would only press a dead handle.
        return e
      }
      try {
        return await next(e)
      } catch (error) {
        if (!releasedPressHandle(error)) throw error
        return e
      }
    }
    if (e.element === 'back-to-main') {
      try {
        await $.ui.view({})
        switched = undefined
        // The return re-rendered the pane and released the pressed element's
        // handler; the button's own press only refreshes the render, so the
        // hook folds that refresh in instead of handing the press down.
        $.ui.invalidate('ui.render')
        return e
      } catch {
        // The host refused the return; the pane keeps marking the child in view.
      }
    }
    try {
      return await next(e)
    } catch (error) {
      if (!releasedPressHandle(error)) throw error
      return e
    }
  })
}
