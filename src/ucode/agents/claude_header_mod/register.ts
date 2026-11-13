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

type LanguageTransportState = {
  ownsHints: boolean;
  warned: boolean;
  transport?: { upstream: string; url: string };
};

async function restoreHints($: EngineInterface, state: LanguageTransportState): Promise<void> {
  if (!state.ownsHints) return;
  if (await $.env.get('CLAUDE_CODE_GATEWAY_HINT_HEADERS') === '1') {
    const { env } = await $.settings.read();
    const result = await $.env.set('CLAUDE_CODE_GATEWAY_HINT_HEADERS', env?.CLAUDE_CODE_GATEWAY_HINT_HEADERS);
    if (result?.deny) return;
  }
  state.ownsHints = false;
}

async function apply($: EngineInterface, state: LanguageTransportState, warn = false): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  const merged = headerLines(
    await $.env.get('ANTHROPIC_CUSTOM_HEADERS'),
    await $.env.get('UG_HEADERS'),
  );
  if (merged !== undefined) await $.env.set('ANTHROPIC_CUSTOM_HEADERS', merged);
  await applyLanguageTransport($, state, warn);
}

async function applyLanguageTransport($: EngineInterface, state: LanguageTransportState, warn: boolean): Promise<void> {
  let upstream = state.transport?.upstream;
  let url = state.transport?.url;
  let cleanHeaders: string | undefined;
  let temporarilyUnavailable = false;
  try {
    const raw = await $.env.get('UG_CLAUDE_LANGUAGE_PROXY');
    if (!raw && !state.transport) {
      await restoreHints($, state);
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw ?? '');
    } catch {
      parsed = undefined;
    }
    const headers = (await $.env.get('ANTHROPIC_CUSTOM_HEADERS') ?? '')
      .split(/\r?\n/).filter(line => !/^\s*x-ug-(response-language|language-session)\s*:/i.test(line));
    cleanHeaders = headers.filter(Boolean).join('\n');
    const settings = await $.settings.read();
    // This descriptor is launch-only. Settings env is lower-trust input to an organization mod.
    if (settings.env?.UG_CLAUDE_LANGUAGE_PROXY !== undefined) {
      throw new Error('Language transport descriptor must come from the launcher');
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      throw new Error('Language transport descriptor is invalid');
    }
    const transport = parsed as { url?: unknown; upstream?: unknown; secret?: unknown };
    if (
      typeof transport.url !== 'string' ||
      !/^http:\/\/127\.0\.0\.1:\d+\/ai-gateway\/anthropic$/.test(transport.url) ||
      typeof transport.upstream !== 'string' ||
      typeof transport.secret !== 'string' || !transport.secret
    ) {
      throw new Error('Language transport descriptor is invalid');
    }
    upstream = transport.upstream;
    url = transport.url;
    state.transport = { upstream, url };
    const base = await $.env.get('ANTHROPIC_BASE_URL');
    if (base !== transport.upstream && base !== transport.url) {
      await $.env.set('ANTHROPIC_CUSTOM_HEADERS', cleanHeaders ?? '');
      await restoreHints($, state);
      return;
    }
    const hints = await $.env.get('CLAUDE_CODE_GATEWAY_HINT_HEADERS');
    if (hints !== undefined && hints !== '1') {
      if (warn) $.ui.log('Skipping optional language inheritance because gateway identity headers are disabled.');
      await $.env.set('ANTHROPIC_CUSTOM_HEADERS', cleanHeaders ?? '');
      if (base === transport.url) await $.env.set('ANTHROPIC_BASE_URL', transport.upstream);
      state.ownsHints = false;
      return;
    }
    const health = await $.http.fetch(`${transport.url}/_ug_language_health`, {
      headers: { 'X-UG-Language-Session': transport.secret },
    });
    if (!health.ok) {
      temporarilyUnavailable = health.status === 503;
      throw new Error('Language transport is unavailable');
    }
    const { language } = settings;
    const configured = typeof language === 'string' && language.trim() !== '';
    if (configured) {
      headers.push(`X-UG-Response-Language: ${encodeURIComponent(language.trim())}`);
    }
    headers.push(`X-UG-Language-Session: ${transport.secret}`);
    await $.env.set('ANTHROPIC_CUSTOM_HEADERS', headers.filter(Boolean).join('\n'));
    await $.env.set('ANTHROPIC_BASE_URL', transport.url);
    if (hints === undefined) {
      const result = await $.env.set('CLAUDE_CODE_GATEWAY_HINT_HEADERS', '1');
      if (result?.deny) throw new Error('Gateway identity headers are unavailable');
      state.ownsHints = true;
    }
    state.warned = false;
  } catch {
    // Optional inheritance must not reject a turn when native APIs are unavailable.
    try {
      if (!temporarilyUnavailable) await $.env.set('UG_CLAUDE_LANGUAGE_PROXY', '');
      if (cleanHeaders !== undefined) await $.env.set('ANTHROPIC_CUSTOM_HEADERS', cleanHeaders);
    } catch {}
    try {
      await restoreHints($, state);
    } catch {}
    try {
      if (upstream && url && await $.env.get('ANTHROPIC_BASE_URL') === url) {
        await $.env.set('ANTHROPIC_BASE_URL', upstream);
      }
    } catch {}
    if (!state.warned) {
      state.warned = true;
      try {
        $.ui.log('Optional subagent language inheritance is unavailable; continuing without it.', { to: warn ? 'transcript' : 'debug' });
      } catch {}
    }
  }
}

export function register(on: On): void {
  const state: LanguageTransportState = { ownsHints: false, warned: false };
  on('session.start', async ($, e, next) => {
    await apply($, state, true);
    return next(e);
  });
  // Claude rebuilds its client per request and a settings reload resets the variable, so add the
  // headers again before each one.
  on('turn.step', async function* ($, e, next) {
    await apply($, state);
    return yield* next(e);
  });
}
