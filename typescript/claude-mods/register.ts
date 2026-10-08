import type { Register } from 'claude-code'

import { registerStatusBand } from './smart-routing-status'

// Entry for ug's smart-routing UI mod. Claude Code loads one hooks module per
// plugin, so this file imports and composes the concerns; today just the status
// band above the prompt, with more (e.g. the per-subagent routing line) importing
// here. Written into the launch-scoped routing plugin under
// ENABLE_CLAUDE_CODE_MODS (see ucode.mods); visualization only, ug's Python hooks
// still route.
export const register: Register = on => {
  registerStatusBand(on)
}
