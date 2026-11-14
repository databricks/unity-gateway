import type { EngineInterface, On } from 'claude-code';

// Adds the launch's `UG_HEADERS` lines (`ug --header`) to `ANTHROPIC_CUSTOM_HEADERS` for this
// process only. ug installs it as the organization's mod, the one tier allowed to change a variable
// managed settings set. A header whose name is already present is left alone, so neither ug's nor an
// admin's header can be replaced from a launch. It also supplies language context for routed sessions.

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

async function routingEnabled($: EngineInterface): Promise<boolean> {
  const environment: Record<string, unknown> = {
    ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2'),
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
  };
  const sessionFile = await $.env.get('UCODE_SESSION_ENV_FILE');
  if (sessionFile) {
    try {
      const controls = JSON.parse(await $.fs.read(sessionFile));
      for (const key of Object.keys(environment)) {
        if (typeof controls?.[key] === 'string') environment[key] = controls[key];
      }
    } catch {
      // Match the routing hooks' inherited-environment fallback for unreadable controls.
    }
  }
  return environment.ENABLE_SMART_ROUTING_V2 === '1' || environment.ENABLE_SMART_ROUTING_SUBAGENT_ONLY === '1';
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
  let submittedLanguage: string | undefined;
  let routingActive: boolean | undefined;
  on('session.start', async ($, e, next) => {
    submittedLanguage = undefined;
    routingActive = await routingEnabled($);
    await apply($);
    return next(e);
  });
  on('session.end', { reason: ['clear', 'resume'] }, ($, e, next) => {
    submittedLanguage = undefined;
    routingActive = undefined;
    return next(e);
  });
  // Shared context requires managed trust and reaches child progress-summary forks.
  on('prompt.context', async ($, e, next) => {
    const result = await next(e);
    if (!await routingEnabled($)) return result;
    return { ...result, blocks: [...result.blocks,
      { name: 'responseLanguage', text: await responseLanguage($) },
    ] };
  });
  on('prompt.submit', { text: /[\s\S]*/ }, async ($, e, next) => {
    if (!await routingEnabled($)) return next(e);
    const language = await responseLanguage($);
    const changed = submittedLanguage !== undefined && submittedLanguage !== language;
    submittedLanguage = language;
    if (!changed) return next(e);
    $.ui.invalidate('prompt.context');
    return next({ ...e, context: [...(e.context ?? []), language] });
  });
  // Claude rebuilds its client per request and a settings reload resets the variable, so add the
  // headers again before each one.
  on('turn.step', async function* ($, e, next) {
    const enabled = await routingEnabled($);
    if (routingActive !== undefined && enabled !== routingActive) $.ui.invalidate('prompt.context');
    routingActive = enabled;
    await apply($);
    return yield* next(e);
  });
}
