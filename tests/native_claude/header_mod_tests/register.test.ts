import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { headerLines } from '../hooks/register.ts';

const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

function setup(on: On, env: Record<string, string | undefined>) {
  on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: '' } }));
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    env[e.name] = e.value;
    return { value: undefined };
  });
  // Stands in for Claude's own handling beneath the mod.
  on('session.start', ($, e) => ({ cwd: e.cwd }));
  return env;
}

test('session start adds the launch headers after the managed ones', async ($, on) => {
  const env = setup(on, {
    ANTHROPIC_CUSTOM_HEADERS: 'x-databricks-use-coding-agent-mode: true',
    UG_HEADERS: 'x-databricks-traffic-id: testenv://liteswap/pod-1',
  });

  await $.session.start(start);

  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe(
    'x-databricks-use-coding-agent-mode: true\nx-databricks-traffic-id: testenv://liteswap/pod-1',
  );
});

test('a header already sent keeps its value, and reapplying changes nothing', () => {
  const base = 'X-Team: eng\nUser-Agent: ucode/1';
  const merged = headerLines(base, 'x-team: other\nX-New: 1');
  expect(merged).toBe('X-Team: eng\nUser-Agent: ucode/1\nX-New: 1');
  expect(headerLines(merged, 'x-team: other\nX-New: 1')).toBeUndefined();
});

test('malformed and repeated launch lines are skipped, and CRLF bases are read', () => {
  const extra = 'no colon\nbad name: 1\nX-Empty:  \nX-Ctl: a\u0001b\nX-New: 1\nx-new: 2';
  expect(headerLines('X-Team: eng\r\nUser-Agent: ucode/1', extra)).toBe(
    'X-Team: eng\nUser-Agent: ucode/1\nX-New: 1',
  );
  expect(headerLines('X-Team: eng', 'no colon\nbad name: 1')).toBeUndefined();
});

test('without launch headers the variable is left alone', async ($, on) => {
  const env = setup(on, { ANTHROPIC_CUSTOM_HEADERS: 'X-Team: eng' });

  await $.session.start(start);

  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Team: eng');
  expect(headerLines(undefined, '\n')).toBeUndefined();
});

test('language transport follows settings and reloads across provider switches', async ($, on) => {
  const upstream = 'https://workspace.example/ai-gateway/anthropic';
  const url = 'http://127.0.0.1:12345/ai-gateway/anthropic';
  const env = setup(on, {
    ANTHROPIC_BASE_URL: 'https://other.example',
    ANTHROPIC_CUSTOM_HEADERS: 'X-Team: eng',
    UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({ url, upstream, secret: 'session-secret' }),
  });
  let language: unknown = 'English';
  on('settings.read', () => ({ value: { language } }));
  on('turn.step', async function* ($, event) {
    return { turnId: event.turnId, index: event.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null };
  });

  await $.session.start(start);
  expect(env.ANTHROPIC_BASE_URL).toBe('https://other.example');
  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Team: eng');

  env.ANTHROPIC_BASE_URL = upstream;
  for (const value of ['English', '中文', 'Português', '', undefined]) {
    language = value;
    await $.session.start(start);
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain('X-Team: eng');
    expect(env.ANTHROPIC_BASE_URL).toBe(url);
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain('X-UG-Language-Session: session-secret');
    if (value) {
      expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain(`X-UG-Response-Language: ${encodeURIComponent(value)}`);
    } else {
      expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Team: eng\nX-UG-Language-Session: session-secret');
    }
  }

  language = 'English';
  env.ANTHROPIC_BASE_URL = upstream;
  delete env.ANTHROPIC_CUSTOM_HEADERS;
  await $.session.start(start);
  for (const value of ['中文', undefined, 'Português']) {
    language = value;
    env.ANTHROPIC_BASE_URL = upstream;
    env.ANTHROPIC_CUSTOM_HEADERS = 'X-Team: eng';
    const stream = $.turn.step({turnId: 'turn', index: 1, model: 'parent-model', messageCount: 1, agentId: undefined});
    while (!(await stream.next()).done) { /* Drain the native turn. */ }
    expect(env.ANTHROPIC_BASE_URL).toBe(url);
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain('X-Team: eng');
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain('X-UG-Language-Session: session-secret');
    if (value) expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain(`X-UG-Response-Language: ${encodeURIComponent(value)}`);
    else expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Team: eng\nX-UG-Language-Session: session-secret');
  }

  expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain('X-UG-Language-Session: session-secret');
  env.ANTHROPIC_BASE_URL = 'https://other.example';
  await $.session.start(start);
  expect(env.ANTHROPIC_BASE_URL).toBe('https://other.example');
  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Team: eng');
  expect(env.CLAUDE_CODE_GATEWAY_HINT_HEADERS).toBeUndefined();
});

for (const [name, raw] of [
  ['malformed JSON', '{"url":"http://127.0.0.1:12345/ai-gateway/anthropic","secret":"private-session"'],
  ['non-loopback URL', JSON.stringify({
    url: 'https://other.example/ai-gateway/anthropic',
    upstream: 'https://workspace.example/ai-gateway/anthropic', secret: 'private-session',
  })],
  ['missing upstream', JSON.stringify({
    url: 'http://127.0.0.1:12345/ai-gateway/anthropic', secret: 'private-session',
  })],
]) {
  test(`invalid language transport (${name}) is cleared without breaking injected headers`, async ($, on) => {
    const upstream = 'https://workspace.example/ai-gateway/anthropic';
    const env = setup(on, {
      ANTHROPIC_BASE_URL: upstream,
      ANTHROPIC_CUSTOM_HEADERS:
        'X-Required: keep\nX-Team: eng\nX-UG-Response-Language: private-language\nX-UG-Language-Session: private-session',
      UG_HEADERS: 'X-Added: yes',
      UG_CLAUDE_LANGUAGE_PROXY: raw,
    });
    await $.session.start(start);
    expect(env.ANTHROPIC_BASE_URL).toBe(upstream);
    expect(env.UG_CLAUDE_LANGUAGE_PROXY).toBe('');
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Required: keep\nX-Team: eng\nX-Added: yes');
    expect(env.ANTHROPIC_CUSTOM_HEADERS).not.toContain('private-session');
    expect(env.ANTHROPIC_CUSTOM_HEADERS).not.toContain('private-language');
    expect(env.ANTHROPIC_CUSTOM_HEADERS).not.toContain('127.0.0.1');
  });
}

test('disabled identity headers skip language inheritance and restore the upstream', async ($, on) => {
  const upstream = 'https://workspace.example/ai-gateway/anthropic';
  const url = 'http://127.0.0.1:12345/ai-gateway/anthropic';
  const env = setup(on, {
    ANTHROPIC_BASE_URL: url,
    ANTHROPIC_CUSTOM_HEADERS:
      'X-Required: keep\nX-Team: eng\nX-UG-Response-Language: stale\nX-UG-Language-Session: stale-key',
    CLAUDE_CODE_GATEWAY_HINT_HEADERS: '0',
    UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({ url, upstream, secret: 'session-secret' }),
  });
  on('settings.read', () => ({ value: { language: 'English' } }));
  const warnings: string[] = [];
  on('ui.log', ($, e) => { warnings.push(e.text); return { value: undefined }; });
  await $.session.start(start);
  expect(env.ANTHROPIC_BASE_URL).toBe(upstream);
  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Required: keep\nX-Team: eng');
  expect(env.ANTHROPIC_CUSTOM_HEADERS).not.toContain('session-secret');
  expect(env.CLAUDE_CODE_GATEWAY_HINT_HEADERS).toBe('0');
  expect(warnings).toEqual(['Skipping optional language inheritance because gateway identity headers are disabled.']);
  env.CLAUDE_CODE_GATEWAY_HINT_HEADERS = '1';
  await $.session.start(start);
  expect(env.ANTHROPIC_BASE_URL).toBe(url);
  expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain('X-UG-Response-Language: English');
  expect(warnings.length).toBe(1);
});

for (const failure of ['settings', 'env-get', 'env-set', 'env-denied'] as const) {
  test(`optional ${failure} failure restores the upstream and lets the turn proceed`, async ($, on) => {
    const upstream = 'https://workspace.example/ai-gateway/anthropic';
    const url = 'http://127.0.0.1:12345/ai-gateway/anthropic';
    const env: Record<string, string | undefined> = {
      ANTHROPIC_BASE_URL: url,
      ANTHROPIC_CUSTOM_HEADERS: 'X-Required: keep\nX-UG-Language-Session: stale',
      UG_HEADERS: 'X-Added: yes',
      UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({ url, upstream, secret: 'session-secret' }),
    };
    let fail = false;
    on('session.start', ($, e) => ({ cwd: e.cwd }));
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: '' } }));
    on('settings.read', () => {
      if (fail && failure === 'settings') throw new Error('settings unavailable');
      return { value: { language: 'English' } };
    });
    on('env.get', ($, e) => {
      if (fail && failure === 'env-get' && e.name === 'CLAUDE_CODE_GATEWAY_HINT_HEADERS') {
        throw new Error('environment read unavailable');
      }
      return { value: env[e.name] };
    });
    on('env.set', ($, e) => {
      if (fail && failure === 'env-set' && e.name === 'CLAUDE_CODE_GATEWAY_HINT_HEADERS') {
        throw new Error('environment write unavailable');
      }
      if (fail && failure === 'env-denied' && e.name === 'CLAUDE_CODE_GATEWAY_HINT_HEADERS') {
        return { deny: 'environment write denied by policy' };
      }
      env[e.name] = e.value;
      return { value: undefined };
    });
    let turns = 0;
    on('turn.step', async function* ($, e) {
      turns++;
      return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null };
    });
    await $.session.start(start);
    expect(env.ANTHROPIC_BASE_URL).toBe(url);
    fail = true;
    env.CLAUDE_CODE_GATEWAY_HINT_HEADERS = undefined;
    const stream = $.turn.step({turnId: 'turn', index: 1, model: 'parent-model', messageCount: 1, agentId: undefined});
    while (!(await stream.next()).done) { /* Drain the native turn. */ }
    expect(turns).toBe(1);
    expect(env.ANTHROPIC_BASE_URL).toBe(upstream);
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Required: keep\nX-Added: yes');
  });
}

for (const failure of ['unavailable', 'expired credential'] as const) {
  test(`${failure} language transport restores direct routing without replaying a turn`, async ($, on) => {
    const upstream = 'https://workspace.example/ai-gateway/anthropic';
    const url = 'http://127.0.0.1:12345/ai-gateway/anthropic';
    const env: Record<string, string | undefined> = {
      ANTHROPIC_BASE_URL: upstream, ANTHROPIC_CUSTOM_HEADERS: 'X-Required: keep',
      UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({ url, upstream, secret: 'session-secret' }),
    };
    on('env.get', ($, e) => ({ value: env[e.name] }));
    on('env.set', ($, e) => { env[e.name] = e.value; return { value: undefined }; });
    on('session.start', ($, e) => ({ cwd: e.cwd }));
    on('settings.read', () => ({ value: { language: 'English' } }));
    let checks = 0;
    let recovered = false;
    const warnings: string[] = [];
    on('ui.log', ($, e) => { expect(e.to).toBe('debug'); warnings.push(e.text); return { value: undefined }; });
    on('http.fetch', ($, e) => {
      expect(e.url).toBe(`${url}/_ug_language_health`);
      expect(e.init.headers).toEqual({ 'X-UG-Language-Session': 'session-secret' });
      if (++checks === 1 || recovered) return { value: { status: 200, ok: true, headers: {}, text: '' } };
      if (failure === 'unavailable') throw new Error('Connection refused');
      return { value: { status: 503, ok: false, headers: {}, text: '' } };
    });
    let turns = 0;
    on('turn.step', async function* ($, e) {
      turns++;
      expect(env.ANTHROPIC_BASE_URL).toBe(recovered ? url : upstream);
      if (!recovered) {
        expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Required: keep');
        expect(env.CLAUDE_CODE_GATEWAY_HINT_HEADERS).toBeUndefined();
      }
      return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null };
    });
    await $.session.start(start);
    expect(env.ANTHROPIC_BASE_URL).toBe(url);
    for (let index = 0; index < 2; index++) {
      const stream = $.turn.step({ turnId: 'turn', index, model: 'parent-model', messageCount: 1, agentId: undefined });
      while (!(await stream.next()).done) { /* Drain the native turn. */ }
    }
    expect(turns).toBe(2);
    expect(checks).toBe(failure === 'unavailable' ? 2 : 3);
    expect(warnings).toEqual(['Optional subagent language inheritance is unavailable; continuing without it.']);
    if (failure === 'unavailable') {
      expect(env.UG_CLAUDE_LANGUAGE_PROXY).toBe('');
    } else {
      expect(env.UG_CLAUDE_LANGUAGE_PROXY).toBe(JSON.stringify({ url, upstream, secret: 'session-secret' }));
      recovered = true;
      const stream = $.turn.step({ turnId: 'turn', index: 2, model: 'parent-model', messageCount: 1, agentId: undefined });
      while (!(await stream.next()).done) { /* Drain the next native turn; no replay. */ }
      expect(turns).toBe(3);
      expect(checks).toBe(4);
      expect(env.CLAUDE_CODE_GATEWAY_HINT_HEADERS).toBe('1');
      expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain('X-UG-Response-Language: English');
      expect(warnings.length).toBe(1);
    }
  });
}

for (const [name, inherited, configured] of [
  ['inherited enable', '1', undefined],
  ['settings enable after activation', undefined, '1'],
  ['settings disable after activation', undefined, '0'],
] as const) {
  test(`fallback preserves ${name} of gateway identity headers`, async ($, on) => {
    const upstream = 'https://workspace.example/ai-gateway/anthropic';
    const url = 'http://127.0.0.1:12345/ai-gateway/anthropic';
    const env: Record<string, string | undefined> = {
      ANTHROPIC_BASE_URL: upstream,
      CLAUDE_CODE_GATEWAY_HINT_HEADERS: inherited,
      UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({ url, upstream, secret: 'session-secret' }),
    };
    on('env.get', ($, e) => ({ value: env[e.name] }));
    on('env.set', ($, e) => { env[e.name] = e.value; return { value: undefined }; });
    on('session.start', ($, e) => ({ cwd: e.cwd }));
    let settingsHint: string | undefined;
    let unavailable = false;
    on('http.fetch', () => ({ value: {
      status: unavailable ? 503 : 200, ok: !unavailable, headers: {}, text: '',
    } }));
    on('settings.read', () => ({ value: { language: 'English', env: {
      ...(settingsHint === undefined ? {} : { CLAUDE_CODE_GATEWAY_HINT_HEADERS: settingsHint }),
    } } }));
    await $.session.start(start);
    expect(env.CLAUDE_CODE_GATEWAY_HINT_HEADERS).toBe('1');
    settingsHint = configured;
    if (configured !== undefined) env.CLAUDE_CODE_GATEWAY_HINT_HEADERS = configured;
    unavailable = true;
    await $.session.start(start);
    expect(env.ANTHROPIC_BASE_URL).toBe(upstream);
    expect(env.CLAUDE_CODE_GATEWAY_HINT_HEADERS).toBe(configured ?? inherited);
    expect(env.ANTHROPIC_CUSTOM_HEADERS ?? '').not.toContain('session-secret');
  });
}

for (const [afterActivation, corruption] of [[false, 'forged'], [true, 'forged'], [true, ''], [true, '{']] as const) {
  test(`settings cannot replace the launch descriptor ${afterActivation ? 'after activation' : 'at startup'} with ${JSON.stringify(corruption)}`, async ($, on) => {
    const upstream = 'https://workspace.example/ai-gateway/anthropic';
    const url = 'http://127.0.0.1:12345/ai-gateway/anthropic';
    const attacker = 'http://127.0.0.1:23456/ai-gateway/anthropic';
    const launch = JSON.stringify({ url, upstream, secret: 'launch-secret' });
    // The second case also tries to turn fallback itself into an attacker redirect.
    const forged = corruption === 'forged' ? JSON.stringify({ url: afterActivation ? url : attacker, upstream: afterActivation ? attacker : upstream, secret: 'forged-secret' }) : corruption;
    let configured = afterActivation ? undefined : forged;
    const env: Record<string, string | undefined> = {
      ANTHROPIC_BASE_URL: upstream,
      ANTHROPIC_CUSTOM_HEADERS: 'X-Required: keep',
      UG_CLAUDE_LANGUAGE_PROXY: configured ?? launch,
    };
    let checks = 0;
    on('env.get', ($, e) => ({ value: env[e.name] }));
    on('env.set', ($, e) => { env[e.name] = e.value; return { value: undefined }; });
    on('settings.read', () => ({ value: { env: { UG_CLAUDE_LANGUAGE_PROXY: configured } } }));
    on('session.start', ($, e) => ({ cwd: e.cwd }));
    on('http.fetch', ($, e) => {
      checks++;
      expect(e.url).toBe(`${url}/_ug_language_health`);
      return { value: { status: 200, ok: true, headers: {}, text: '' } };
    });
    await $.session.start(start);
    if (afterActivation) {
      expect(env.ANTHROPIC_BASE_URL).toBe(url);
      configured = forged;
      env.UG_CLAUDE_LANGUAGE_PROXY = forged;
      await $.session.start(start);
    }
    expect(checks).toBe(afterActivation ? 1 : 0);
    expect(env.ANTHROPIC_BASE_URL).toBe(upstream);
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Required: keep');
    expect(env.UG_CLAUDE_LANGUAGE_PROXY).toBe('');
    expect(env.CLAUDE_CODE_GATEWAY_HINT_HEADERS).toBeUndefined();
  });
}
