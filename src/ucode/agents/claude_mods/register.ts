import type { On, PluginOptions } from 'claude-code';

/** Entry point for UG's in-process Claude hooks. */
export function register(on: On, _options: PluginOptions): void {
  on('session.start', ($, e, next) => next(e));
}
