import type { Register } from 'claude-code'

import { registerSavings } from './smart-routing-savings'
import { registerStatusBand } from './smart-routing-status'
import { registerSubagentRouting } from './subagent-routing'

// Entry for ug's smart-routing UI mod. Claude Code loads one hooks module per
// plugin, so this file imports and composes the concerns: the status band above
// the prompt, the compact per-subagent routing line, and the savings estimate the
// band appends. Written into the launch-scoped routing plugin under
// ENABLE_CLAUDE_CODE_MODS (see ucode.mods); visualization only, ug's Python hooks
// still route.
export const register: Register = on => {
  registerStatusBand(on)
  registerSubagentRouting(on)
  registerSavings(on)
}
