import type { On, PluginOptions } from 'claude-code';

import { registerSavings, savingsSessionStart } from './savings';

/** Entry point for UG's in-process Claude hooks. */
export function register(on: On, options: PluginOptions): void {
  on('session.start', savingsSessionStart);
  registerSavings(on, options);
}
