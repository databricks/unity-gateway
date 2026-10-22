import type { EngineInterface, On } from 'claude-code';

// Adds the launch's `UG_HEADERS` lines (`ug --header`) to `ANTHROPIC_CUSTOM_HEADERS` for this
// process only. ug installs it as the organization's mod, the one tier allowed to change a variable
// managed settings set. A header whose name is already present is left alone, so neither ug's nor an
// admin's header can be replaced from a launch.
export function headerLines(base: string | undefined, extra: string | undefined): string | undefined {
  const added = (extra ?? '').split('\n').filter((line) => line.trim());
  if (!added.length) return undefined;
  const lines = (base ?? '').split('\n').filter((line) => line.trim());
  const name = (line: string) => line.split(':', 1)[0].trim().toLowerCase();
  const present = new Set(lines.map(name));
  const merged = [...lines, ...added.filter((line) => !present.has(name(line)))];
  return merged.length === lines.length ? undefined : merged.join('\n');
}

async function apply($: EngineInterface): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  const merged = headerLines(
    await $.env.get('ANTHROPIC_CUSTOM_HEADERS'),
    await $.env.get('UG_HEADERS'),
  );
  if (merged !== undefined) await $.env.set('ANTHROPIC_CUSTOM_HEADERS', merged);
}

export function register(on: On): void {
  on('session.start', async ($, e, next) => {
    await apply($);
    return next(e);
  });
  // Claude rebuilds its client per request and a settings reload resets the variable, so add the
  // headers again before each one.
  on('turn.step', async function* ($, e, next) {
    await apply($);
    return yield* next(e);
  });
}
