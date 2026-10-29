import type { EngineInterface, On } from 'claude-code';

// Adds the launch's `UG_HEADERS` lines (`ug --header`) to `ANTHROPIC_CUSTOM_HEADERS` for this
// process only. ug installs it as the organization's mod, the one tier allowed to change a variable
// managed settings set. A header whose name is already present is left alone, so neither ug's nor an
// admin's header can be replaced from a launch.

const HEADER_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;

// `Name: value` with an HTTP token name, a non-empty value, and no control characters but tab.
function isHeaderLine(line: string): boolean {
  const colon = line.indexOf(':');
  return (
    colon > 0 &&
    HEADER_NAME.test(line.slice(0, colon)) &&
    line.slice(colon + 1).trim() !== '' &&
    !/[\x00-\x08\x0a-\x1f\x7f]/.test(line)
  );
}

export function headerLines(base: string | undefined, extra: string | undefined): string | undefined {
  const lines = (base ?? '').split(/\r?\n/).filter((line) => line.trim());
  const name = (line: string) => line.split(':', 1)[0].trim().toLowerCase();
  const present = new Set(lines.map(name));
  const merged = [...lines];
  // Malformed lines are skipped: launched without ug, nothing else has checked them.
  for (const line of (extra ?? '').split(/\r?\n/).map((l) => l.trim())) {
    if (!isHeaderLine(line) || present.has(name(line))) continue;
    present.add(name(line));
    merged.push(line);
  }
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
