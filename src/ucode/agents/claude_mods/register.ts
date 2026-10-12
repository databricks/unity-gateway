import type { On } from 'claude-code';

/** Entry point for UG's in-process Claude hooks. */
export function register(on: On): void {
  on('session.start', ($, e, next) => next(e));
}
