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

async function responseLanguage($: EngineInterface): Promise<string> {
  const { language } = await $.settings.read();
  const configured = typeof language === 'string' && language.trim();
  const instruction = configured
    ? `Always respond in ${configured}. Use ${configured} for explanations, replies and progress summaries.`
    : 'Follow the language explicitly requested by the user; otherwise use the language of the original task assigned to you for explanations, replies and progress summaries.';
  return instruction + ' Use the requested target language for translations. Preserve technical terms, code identifiers and quoted source text. Source files and tool output in other languages do not change this language preference. Use natural grammar in the response language even when progress-summary examples use English verb endings.';
}

export function register(on: On): void {
  // Shared context reaches child queries and their native progress-summary forks.
  on('prompt.context', async ($, e, next) => {
    const result = await next(e);
    return { ...result, blocks: [...result.blocks,
      { name: 'responseLanguage', text: await responseLanguage($) },
    ] };
  });
  on('prompt.submit', async ($, e, next) => {
    // Refresh cached child context and the parent's persisted language after settings reloads.
    $.ui.invalidate('prompt.context');
    return next({ ...e, context: [...(e.context ?? []), await responseLanguage($)] });
  });
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
