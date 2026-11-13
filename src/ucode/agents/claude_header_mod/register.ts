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

async function apply($: EngineInterface, warn = false): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  const merged = headerLines(
    await $.env.get('ANTHROPIC_CUSTOM_HEADERS'),
    await $.env.get('UG_HEADERS'),
  );
  if (merged !== undefined) await $.env.set('ANTHROPIC_CUSTOM_HEADERS', merged);
  await applyLanguageTransport($, warn);
}

async function applyLanguageTransport($: EngineInterface, warn: boolean): Promise<void> {
  const raw = await $.env.get('UG_CLAUDE_LANGUAGE_PROXY');
  if (!raw) return;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = undefined;
  }
  const headers = (await $.env.get('ANTHROPIC_CUSTOM_HEADERS') ?? '')
    .split(/\r?\n/).filter(line => !/^\s*x-ug-(response-language|language-session)\s*:/i.test(line));
  const clearInvalidTransport = async () => {
    await $.env.set('UG_CLAUDE_LANGUAGE_PROXY', '');
    await $.env.set('ANTHROPIC_CUSTOM_HEADERS', headers.filter(Boolean).join('\n'));
  };
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    await clearInvalidTransport();
    return;
  }
  const transport = parsed as { url?: unknown; upstream?: unknown; secret?: unknown };
  if (
    typeof transport.url !== 'string' ||
    !/^http:\/\/127\.0\.0\.1:\d+\/ai-gateway\/anthropic$/.test(transport.url) ||
    typeof transport.upstream !== 'string' ||
    typeof transport.secret !== 'string' || !transport.secret
  ) {
    await clearInvalidTransport();
    return;
  }
  const base = await $.env.get('ANTHROPIC_BASE_URL');
  if (base !== transport.upstream && base !== transport.url) {
    await $.env.set('ANTHROPIC_CUSTOM_HEADERS', headers.filter(Boolean).join('\n'));
    return;
  }
  const hints = await $.env.get('CLAUDE_CODE_GATEWAY_HINT_HEADERS');
  if (hints !== undefined && hints !== '1') {
    if (warn) $.ui.log('Skipping optional language inheritance because gateway identity headers are disabled.');
    if (base === transport.url) await $.env.set('ANTHROPIC_BASE_URL', transport.upstream);
    await $.env.set('ANTHROPIC_CUSTOM_HEADERS', headers.filter(Boolean).join('\n'));
    return;
  }
  const { language } = await $.settings.read();
  const configured = typeof language === 'string' && language.trim() !== '';
  if (configured) {
    headers.push(`X-UG-Response-Language: ${encodeURIComponent(language.trim())}`);
  }
  headers.push(`X-UG-Language-Session: ${transport.secret}`);
  await $.env.set('ANTHROPIC_CUSTOM_HEADERS', headers.filter(Boolean).join('\n'));
  await $.env.set('ANTHROPIC_BASE_URL', transport.url);
  if (hints === undefined) {
    await $.env.set('CLAUDE_CODE_GATEWAY_HINT_HEADERS', '1');
  }
}

export function register(on: On): void {
  on('session.start', async ($, e, next) => {
    await apply($, true);
    return next(e);
  });
  // Claude rebuilds its client per request and a settings reload resets the variable, so add the
  // headers again before each one.
  on('turn.step', async function* ($, e, next) {
    await apply($);
    return yield* next(e);
  });
}
