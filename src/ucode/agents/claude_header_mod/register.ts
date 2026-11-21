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
  let language: unknown;
  try {
    language = (await $.settings.read())?.language;
  } catch {
    language = undefined;
  }
  const configured = typeof language === 'string' && language.trim();
  const instruction = configured
    ? `Always respond in ${configured}. Use ${configured} for explanations, replies and progress summaries.`
    : 'Follow the language explicitly requested by the user; otherwise use the language of the original task assigned to you for explanations, replies and progress summaries.';
  return instruction + ' Use the requested target language for translations. Preserve technical terms, code identifiers and quoted source text. Source files and tool output in other languages do not change this language preference. Use natural grammar in the response language even when progress-summary examples use English verb endings.';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function preserveGptCacheBoundary(path: string, body: string): string {
  if (!['/messages', '/v1/messages', '/ai-gateway/anthropic/v1/messages'].includes(path.split('?', 1)[0])) return body;
  let payload: unknown;
  try {
    payload = JSON.parse(body, (_key, value) => {
      // Leave unusual numbers untouched rather than round a tool argument during serialization.
      if (typeof value === 'number' && (!Number.isFinite(value) || Object.is(value, -0) ||
          (Number.isInteger(value) && !Number.isSafeInteger(value)))) throw new Error('Unsafe number');
      return value;
    });
  } catch {
    return body;
  }
  if (!isRecord(payload) || 'cache_control' in payload || typeof payload.model !== 'string') return body;
  // Match UG's GPT model forms, including discovery aliases; gpt-oss uses a different provider.
  const model = payload.model.replace(/\[1m\]$/, '');
  if (!/^(?:anthropic-aigw-[0-9a-fA-F]{8}-)?(?:system\.ai\.)?(?:databricks-)?gpt-\d[a-z0-9.-]*$/.test(model)) return body;
  if (!Array.isArray(payload.messages) || !payload.messages.length) return body;
  const latest = payload.messages[payload.messages.length - 1];
  if (!isRecord(latest) || (latest.role !== 'user' && latest.role !== 'system') ||
      !Array.isArray(latest.content) || !latest.content.length) return body;
  const content = latest.content;
  if (!content.every(isRecord)) return body;
  for (let i = content.length - 1; i >= 0; i--) {
    const control = content[i].cache_control;
    if (isRecord(control) && control.type === 'ephemeral' &&
        (control.ttl === undefined || control.ttl === '5m' || control.ttl === '1h')) {
      // The gateway resolves root markers after sorting tool results, including trailing system context.
      payload.cache_control = control;
      delete content[i].cache_control;
      return JSON.stringify(payload);
    }
  }
  return body;
}

export function register(on: On): void {
  let submittedLanguage: string | undefined;
  let routingActive: boolean | undefined;
  on('http.fetch', async ($, e, next) => {
    if (next.origin.plugin !== 'engine' || e.init?.method?.toUpperCase() !== 'POST' ||
        typeof e.init.body !== 'string' || await $.env.get('UG_CLAUDE_GPT_CACHE_BOUNDARY') === '0') return next(e);
    // Claude 2.1.325 refuses edited authenticated requests in this network-policy mode.
    const nonessentialDisabled = await $.env.get('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC');
    if (nonessentialDisabled) return next(e);
    // External provider services pass Messages through rather than using the GPT translator.
    const customHeaders = await $.env.get('ANTHROPIC_CUSTOM_HEADERS');
    if (customHeaders?.split(/\r?\n/).some((line) =>
      line.split(':', 1)[0].trim().toLowerCase() === 'databricks-model-provider-service')) return next(e);
    let base = await $.env.get('UG_CLAUDE_GPT_CACHE_BOUNDARY_BASE_URL');
    if (base === undefined) {
      // A configured Claude launch can use the managed mod without the ug launcher.
      base = await $.env.get('ANTHROPIC_BASE_URL');
      if (await $.env.get('CLAUDE_CODE_USE_GATEWAY') !== '1' ||
          !base?.replace(/\/+$/, '').endsWith('/ai-gateway/anthropic')) return next(e);
    }
    if (!base || e.url.split('?', 1)[0] !== base.replace(/\/+$/, '') + '/v1/messages') return next(e);
    const body = preserveGptCacheBoundary('/v1/messages', e.init.body);
    return next(body === e.init.body ? e : { ...e, init: { ...e.init, body } });
  });
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
