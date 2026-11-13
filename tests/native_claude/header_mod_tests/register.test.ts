import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { headerLines } from '../hooks/register.ts';

const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

function setup(on: On, env: Record<string, string | undefined>) {
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

test('language transport follows settings changes and preserves other headers', async ($, on) => {
  const upstream = 'https://workspace.example/ai-gateway/anthropic';
  const url = 'http://127.0.0.1:12345/ai-gateway/anthropic';
  const env = setup(on, {
    ANTHROPIC_BASE_URL: upstream,
    ANTHROPIC_CUSTOM_HEADERS: 'X-Team: eng',
    UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({ url, upstream, secret: 'session-secret' }),
  });
  let language: unknown;
  on('settings.read', () => ({ value: { language } }));
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
});

test('switching providers removes private transport headers before returning', async ($, on) => {
  const upstream = 'https://workspace.example/ai-gateway/anthropic';
  const env = setup(on, {
    ANTHROPIC_BASE_URL: upstream,
    ANTHROPIC_CUSTOM_HEADERS: 'X-Team: eng',
    UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({
      url: 'http://127.0.0.1:12345/ai-gateway/anthropic', upstream, secret: 'session-secret',
    }),
  });
  on('settings.read', () => ({ value: { language: 'English' } }));
  await $.session.start(start);
  expect(env.ANTHROPIC_CUSTOM_HEADERS).toContain('X-UG-Language-Session: session-secret');
  env.ANTHROPIC_BASE_URL = 'https://other.example';
  await $.session.start(start);
  expect(env.ANTHROPIC_BASE_URL).toBe('https://other.example');
  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Team: eng');
});

test('malformed language transport is cleared without breaking injected headers', async ($, on) => {
  const upstream = 'https://workspace.example/ai-gateway/anthropic';
  const env = setup(on, {
    ANTHROPIC_BASE_URL: upstream,
    ANTHROPIC_CUSTOM_HEADERS:
      'X-Required: keep\nX-Team: eng\nX-UG-Response-Language: private-language\nX-UG-Language-Session: private-session',
    UG_HEADERS: 'X-Added: yes',
    UG_CLAUDE_LANGUAGE_PROXY:
      '{"url":"http://127.0.0.1:12345/ai-gateway/anthropic","secret":"private-session"',
  });
  await $.session.start(start);
  expect(env.ANTHROPIC_BASE_URL).toBe(upstream);
  expect(env.UG_CLAUDE_LANGUAGE_PROXY).toBe('');
  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Required: keep\nX-Team: eng\nX-Added: yes');
  expect(env.ANTHROPIC_CUSTOM_HEADERS).not.toContain('private-session');
  expect(env.ANTHROPIC_CUSTOM_HEADERS).not.toContain('private-language');
  expect(env.ANTHROPIC_CUSTOM_HEADERS).not.toContain('127.0.0.1');
});

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

test('each turn reapplies the current language after a settings reload', async ($, on) => {
  const upstream = 'https://workspace.example/ai-gateway/anthropic';
  const url = 'http://127.0.0.1:12345/ai-gateway/anthropic';
  const env = setup(on, {
    ANTHROPIC_BASE_URL: upstream,
    UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({ url, upstream, secret: 'session-secret' }),
  });
  let language: string | undefined = 'English';
  on('settings.read', () => ({ value: { language } }));
  on('turn.step', async function* ($, event) {
    return { turnId: event.turnId, index: event.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null };
  });
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
});

test('language transport does not replace another provider endpoint', async ($, on) => {
  const env = setup(on, {
    ANTHROPIC_BASE_URL: 'https://other.example',
    ANTHROPIC_CUSTOM_HEADERS: 'X-Team: eng',
    UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({
      url: 'http://127.0.0.1:12345/ai-gateway/anthropic',
      upstream: 'https://workspace.example/ai-gateway/anthropic', secret: 'session-secret',
    }),
  });
  on('settings.read', () => ({ value: { language: 'English' } }));
  await $.session.start(start);
  expect(env.ANTHROPIC_BASE_URL).toBe('https://other.example');
  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Team: eng');
});

for (const failure of ['settings', 'env-get', 'env-set'] as const) {
  test(`optional ${failure} failure restores the upstream and lets the turn proceed`, async ($, on) => {
    const upstream = 'https://workspace.example/ai-gateway/anthropic';
    const url = 'http://127.0.0.1:12345/ai-gateway/anthropic';
    const env: Record<string, string | undefined> = {
      ANTHROPIC_BASE_URL: url,
      ANTHROPIC_CUSTOM_HEADERS: 'X-Required: keep\nX-UG-Language-Session: stale',
      UG_HEADERS: 'X-Added: yes',
      UG_CLAUDE_LANGUAGE_PROXY: JSON.stringify({ url, upstream, secret: 'session-secret' }),
    };
    on('session.start', ($, e) => ({ cwd: e.cwd }));
    on('settings.read', () => {
      if (failure === 'settings') throw new Error('settings unavailable');
      return { value: { language: 'English' } };
    });
    on('env.get', ($, e) => {
      if (failure === 'env-get' && e.name === 'CLAUDE_CODE_GATEWAY_HINT_HEADERS') {
        throw new Error('environment read unavailable');
      }
      return { value: env[e.name] };
    });
    on('env.set', ($, e) => {
      if (failure === 'env-set' && e.name === 'CLAUDE_CODE_GATEWAY_HINT_HEADERS') {
        throw new Error('environment write unavailable');
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
    const stream = $.turn.step({turnId: 'turn', index: 1, model: 'parent-model', messageCount: 1, agentId: undefined});
    while (!(await stream.next()).done) { /* Drain the native turn. */ }
    expect(turns).toBe(1);
    expect(env.ANTHROPIC_BASE_URL).toBe(upstream);
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Required: keep\nX-Added: yes');
  });
}
