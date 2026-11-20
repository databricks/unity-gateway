import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { headerLines, preserveGptCacheBoundary, register } from '../hooks/register.ts';

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

test('without routing the header mod leaves language and submitted context untouched', async ($, on) => {
  const env = setup(on, { ENABLE_SMART_ROUTER_ORCHESTRATOR: '1' });
  let settingsReads = 0;
  on('settings.read', () => { settingsReads += 1; return { value: { language: '中文' } }; });
  on('prompt.context', ($, e) => ({ blocks: e.blocks }));
  on('prompt.submit', ($, e) => ({ text: e.text, context: e.context }));
  const blocks = [{ name: 'existing', text: 'Existing context' }];
  const prompt = { text: 'Original task', context: ['Existing context'] };
  await $.session.start(start);
  for (const value of [undefined, '0']) {
    env.ENABLE_SMART_ROUTING_V2 = value;
    env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY = value;
    expect(await $.prompt.context({ blocks })).toEqual({ blocks });
    expect(await $.prompt.submit(prompt)).toEqual(prompt);
  }
  expect(settingsReads).toBe(0);
});

test('routed shared context preserves existing blocks and effective language or task-language fallback', async ($, on) => {
  setup(on, { ENABLE_SMART_ROUTING_V2: '1' });
  let language: unknown = 'English';
  let readMode = 'settings';
  on('settings.read', () => {
    if (readMode === 'deny') return { deny: 'Settings unavailable' };
    return { value: { language } };
  });
  on('prompt.submit', ($, e) => ({ text: e.text, context: e.context }));
  const blocks = [{ name: 'existing', text: 'Existing context' }];
  const prompt = { text: 'Original task', context: ['Existing context'] };
  let calls = 0;
  on('prompt.context', ($, e) => {
    calls += 1;
    expect(e.blocks).toEqual(blocks);
    return { blocks: e.blocks };
  });
  await $.session.start(start);
  for (const [value, mode] of [
    ['English', 'settings'], ['中文', 'settings'], ['Português', 'settings'],
    ['', 'settings'], [undefined, 'settings'], [undefined, 'deny'],
  ] as const) {
    language = value;
    readMode = mode;
    const result = await $.prompt.context({ blocks });
    expect(result.blocks).toHaveLength(2);
    expect(result.blocks[0]).toEqual(blocks[0]);
    expect(result.blocks[1].name).toBe('responseLanguage');
    const instruction = result.blocks[1].text;
    if (value) expect(instruction).toContain(`Always respond in ${value}.`);
    else {
      expect(instruction).toContain('language explicitly requested by the user');
      expect(instruction).toContain('language of the original task assigned to you');
      expect(instruction).not.toContain('Always respond in English');
    }
    expect(instruction).toContain('requested target language for translations');
    expect(instruction).toContain('quoted source text');
    expect(instruction).toContain('progress summaries');
    if (mode !== 'settings') expect(await $.prompt.submit(prompt)).toEqual(prompt);
  }
  expect(calls).toBe(6);
});

test('only changed language appends context and lifecycle resets preserve first-prompt deduplication', async ($, on) => {
  setup(on, { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' });
  let language: unknown = 'English';
  let invalidations = 0;
  on('settings.read', () => ({ value: { language } }));
  on('ui.invalidate', ($, e) => {
    expect(e.event).toBe('prompt.context');
    invalidations += 1;
    return { value: undefined };
  });
  on('prompt.context', ($, e) => ({ blocks: e.blocks }));
  on('prompt.submit', ($, e) => ({ text: e.text, context: e.context }));
  on('session.end', ($, e) => ({ sessionId: e.sessionId }));
  await $.session.start(start);
  const prompt = { text: 'Original task', context: ['Existing context'] };
  const shared = await $.prompt.context({ blocks: [] });
  expect(shared.blocks).toHaveLength(1);
  expect(shared.blocks[0].text).toContain('Always respond in English.');
  for (const value of ['English', 'English', ' English ']) {
    language = value;
    expect(await $.prompt.submit(prompt)).toEqual(prompt);
  }
  expect(invalidations).toBe(0);
  for (const value of ['中文', undefined]) {
    language = value;
    const result = await $.prompt.submit(prompt);
    expect(result.text).toBe(prompt.text);
    expect(result.context).toHaveLength(2);
    expect(result.context?.[0]).toBe(prompt.context[0]);
    const instruction = value ? 'Always respond in 中文.' : 'language of the original task assigned to you';
    expect(result.context?.[1]).toContain(instruction);
    expect((await $.prompt.context({ blocks: [] })).blocks[0].text).toContain(instruction);
    expect(await $.prompt.submit(prompt)).toEqual(prompt);
  }
  expect(invalidations).toBe(2);
  await $.session.start(start);
  language = 'Português';
  expect(await $.prompt.submit(prompt)).toEqual(prompt);
  for (const reason of ['clear', 'resume'] as const) {
    await $.session.end({ reason, sessionId: 'language-session', resume: { id: 'language-session' } });
    language = reason;
    expect(await $.prompt.submit(prompt)).toEqual(prompt);
  }
  expect(invalidations).toBe(2);
});

test('external routing controls invalidate language context once per transition and preserve the stream', async ($, on) => {
  setup(on, { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', UCODE_SESSION_ENV_FILE: '/session/env.json' });
  let enabled = true;
  let invalidations = 0;
  on('fs.read', () => ({ value: JSON.stringify({ ENABLE_SMART_ROUTING_SUBAGENT_ONLY: enabled ? '1' : '0' }) }));
  on('settings.read', () => ({ value: { language: 'English' } }));
  on('prompt.context', ($, e) => ({ blocks: e.blocks }));
  on('session.end', () => ({ sessionId: 'routing-session' }));
  on('ui.invalidate', ($, e) => {
    expect(e.event).toBe('prompt.context');
    invalidations += 1;
    return { value: undefined };
  });
  const event = { turnId: 'parent', index: 1, model: 'parent-model', messageCount: 3 };
  const chunk = { kind: 'text', index: 0, text: 'Original answer' } as const;
  const result = { turnId: event.turnId, index: event.index, answer: chunk.text, toolUses: [], stopReason: 'end_turn', usage: null } as const;
  on('turn.step', async function* ($, e) {
    expect(e).toEqual(event);
    yield chunk;
    return result;
  });
  await $.session.start(start);
  for (const [value, count] of [[true, 0], [false, 1], [false, 1], [true, 2], [true, 2]] as const) {
    enabled = value;
    const stream = $.turn.step(event);
    expect(await stream.next()).toEqual({ done: false, value: chunk });
    expect(await stream.next()).toEqual({ done: true, value: result });
    expect(invalidations).toBe(count);
    const context = await $.prompt.context({ blocks: [] });
    if (enabled) expect(context.blocks[0].text).toContain('Always respond in English.');
    else expect(context.blocks).toEqual([]);
  }
  for (const reason of ['clear', 'resume'] as const) {
    await $.session.end({ reason });
    enabled = !enabled;
    const stream = $.turn.step(event);
    expect(await stream.next()).toEqual({ done: false, value: chunk });
    expect(await stream.next()).toEqual({ done: true, value: result });
    expect(invalidations).toBe(2);
  }
});

function userRequestBody(model: unknown, control: unknown): string {
  return JSON.stringify({
    model,
    messages: [{ role: 'user', content: [{ type: 'text', text: 'prompt', cache_control: control }] }],
  });
}

function cacheControls(body: string): unknown[] {
  const request = JSON.parse(body) as {
    cache_control?: unknown;
    messages?: Array<{ content?: unknown }>;
  };
  return [
    ...(request.cache_control === undefined ? [] : [request.cache_control]),
    ...(request.messages ?? []).flatMap(({ content }) =>
      Array.isArray(content)
        ? content.map((block: { cache_control?: unknown }) => block.cache_control)
        : [],
    ),
  ].filter((control) => control !== undefined);
}

function visibleMessages(messages: Array<{ content: Array<Record<string, unknown>>; [key: string]: unknown }>) {
  return messages.map(({ content, ...message }) => ({
    ...message,
    content: content.map((block) =>
      Object.fromEntries(Object.entries(block).filter(([key]) => key !== 'cache_control')),
    ),
  }));
}

test('allowed Messages paths move cache controls and reject other paths', () => {
  const control = { type: 'ephemeral', ttl: '1h' };
  const body = userRequestBody('system.ai.gpt-6-1-terra', control);
  for (const path of [
    '/messages', '/messages?beta=true',
    '/v1/messages', '/v1/messages?beta=true',
    '/ai-gateway/anthropic/v1/messages', '/ai-gateway/anthropic/v1/messages?beta=true',
  ]) {
    const result = JSON.parse(preserveGptCacheBoundary(path, body));
    expect(result.cache_control).toEqual(control);
    expect(result.messages[0].content[0].cache_control).toBeUndefined();
  }
  for (const path of ['/other/messages', '/v1/messages/extra', '/messages-extra?beta=true']) {
    expect(preserveGptCacheBoundary(path, body)).toBe(body);
  }
});

test('only the exact GPT 6.1 Terra and Sol model IDs, bare or deployment-qualified, with optional 1m suffix, qualify', () => {
  const control = { type: 'ephemeral', ttl: '5m' };
  for (const model of [
    'system.ai.gpt-6-1-terra', 'system.ai.gpt-6-1-terra[1m]',
    'system.ai.gpt-6-1-sol', 'system.ai.gpt-6-1-sol[1m]',
    'anthropic-aigw-bf79c578-system.ai.gpt-6-1-terra',
    'anthropic-aigw-bf79c578-system.ai.gpt-6-1-terra[1m]',
    'anthropic-aigw-c4442a48-system.ai.gpt-6-1-sol',
    'anthropic-aigw-c4442a48-system.ai.gpt-6-1-sol[1m]',
  ]) {
    const result = JSON.parse(preserveGptCacheBoundary('/messages', userRequestBody(model, control)));
    expect(result.model).toBe(model);
    expect(result.cache_control).toEqual(control);
  }
  for (const model of [
    'system.ai.gpt-6-sol', 'system.ai.gpt-6-1-sol[5m]',
    'system.ai.gpt-6-1-sol[1m][1m]', 'system.ai.gpt-6-1-terra-extra', 42, null,
    'anthropic-aigw-77df06ea-system.ai.glm-5-3-flash',
    'anthropic-aigw-12e8d1ee-system.ai.gpt-5-6-sol[1m]',
    'anthropic-aigw-2b40afe0-system.ai.gpt-6-2-astra[1m]',
    'xanthropic-aigw-bf79c578-system.ai.gpt-6-1-terra',
    'anthropic-aigw-77df06ea-system.ai.gpt-6-1-terra-extra',
    'anthropic-aigw--system.ai.gpt-6-1-terra',
    'anthropic-aigw-bf79c578-system.ai.gpt-6-1-terra[5m]',
    'prefix-anthropic-aigw-bf79c578-system.ai.gpt-6-1-terra',
    'anthropic-aigw-bf79c578-system.ai.gpt-6-1-terra-suffix',
  ]) {
    const body = userRequestBody(model, control);
    expect(preserveGptCacheBoundary('/messages', body)).toBe(body);
  }
});

test('moves the last eligible marker from out-of-order parallel tool results without changing visible messages', () => {
  const firstControl = { type: 'ephemeral', ttl: '5m' };
  const defaultControl = { type: 'ephemeral' };
  const resultControl = { type: 'ephemeral', ttl: '1h' };
  const selectedControl = { type: 'ephemeral', ttl: '1h' };
  const body = JSON.stringify({
    model: 'system.ai.gpt-6-1-sol[1m]',
    metadata: { request_id: 'req-1' },
    messages: [
      {
        role: 'user', name: 'requester',
        content: [
          { type: 'text', text: 'earlier one', cache_control: firstControl },
          { type: 'text', text: 'earlier two', cache_control: defaultControl },
        ],
      },
      {
        role: 'assistant', stop_reason: 'tool_use',
        content: [
          { type: 'tool_use', id: 'call-a', name: 'read_file', input: { path: 'a.txt' } },
          { type: 'tool_use', id: 'call-b', name: 'read_file', input: { path: 'b.txt' } },
        ],
      },
      {
        role: 'user',
        content: [
          { type: 'tool_result', tool_use_id: 'call-b', content: 'B result', cache_control: resultControl },
          { type: 'tool_result', tool_use_id: 'call-a', content: 'A result', cache_control: selectedControl },
        ],
      },
    ],
  });

  const result = preserveGptCacheBoundary('/v1/messages?beta=true', body);
  const before = JSON.parse(body);
  const after = JSON.parse(result);
  const expected = JSON.parse(body);
  delete expected.messages[2].content[1].cache_control;
  expected.cache_control = selectedControl;
  expect(after).toEqual(expected);
  expect(visibleMessages(after.messages)).toEqual(visibleMessages(before.messages));
  expect(cacheControls(result)).toHaveLength(4);
  expect(after.cache_control).toEqual(selectedControl);
  expect(after.messages[2].content.map((block: { tool_use_id: string }) => block.tool_use_id)).toEqual([
    'call-b', 'call-a',
  ]);
  expect(preserveGptCacheBoundary('/v1/messages', result)).toBe(result);
});

test('retains allowed TTL forms and skips later markers with invalid types or TTLs', () => {
  for (const control of [
    { type: 'ephemeral' }, { type: 'ephemeral', ttl: '5m' }, { type: 'ephemeral', ttl: '1h' },
  ]) {
    const result = JSON.parse(preserveGptCacheBoundary('/messages', userRequestBody('system.ai.gpt-6-1-terra', control)));
    expect(result.cache_control).toEqual(control);
    expect(result.messages[0].content[0].cache_control).toBeUndefined();
  }

  for (const control of [
    { type: 'ephemeral', ttl: '10m' }, { type: 'ephemeral', ttl: null },
    { type: 'ephemeral', ttl: 5 }, { type: 'ephemeral', ttl: false },
    { type: 'other', ttl: '5m' }, null, 'ephemeral', [],
  ]) {
    const body = userRequestBody('system.ai.gpt-6-1-terra', control);
    expect(preserveGptCacheBoundary('/messages', body)).toBe(body);
  }

  const earlier = { type: 'ephemeral', ttl: '5m' };
  const invalidLater = { type: 'ephemeral', ttl: '10m' };
  const body = JSON.stringify({
    model: 'system.ai.gpt-6-1-terra',
    messages: [{
      role: 'user',
      content: [
        { type: 'text', text: 'eligible', cache_control: earlier },
        { type: 'text', text: 'invalid TTL', cache_control: invalidLater },
      ],
    }],
  });
  const result = JSON.parse(preserveGptCacheBoundary('/messages', body));
  expect(result.cache_control).toEqual(earlier);
  expect(result.messages[0].content[0].cache_control).toBeUndefined();
  expect(result.messages[0].content[1].cache_control).toEqual(invalidLater);
  expect(cacheControls(JSON.stringify(result))).toHaveLength(2);
});

test('returns malformed, ineligible, and root-cache-controlled bodies byte-for-byte', () => {
  const malformed = ['{', '[]', 'null', '"body"', '17', 'true'];
  const ineligible = [
    { model: 'system.ai.gpt-6-1-terra', messages: [] },
    { model: 'system.ai.gpt-6-1-terra', messages: 'not a list' },
    { model: 'system.ai.gpt-6-1-terra', messages: [null] },
    { model: 'system.ai.gpt-6-1-terra', messages: [{ role: 'assistant', content: [{ cache_control: { type: 'ephemeral' } }] }] },
    { model: 'system.ai.gpt-6-1-terra', messages: [{ role: 'user', content: [{ type: 'text', text: 'keep' }] }, { role: 'system', content: [{ type: 'text', text: 'environment', cache_control: { type: 'ephemeral', ttl: '1h' } }] }] },
    { model: 'system.ai.gpt-6-1-terra', messages: [{ role: 'user', content: [] }] },
    { model: 'system.ai.gpt-6-1-terra', messages: [{ role: 'user', content: 'not a list' }] },
    { model: 'system.ai.gpt-6-1-terra', messages: [{ role: 'user', content: [null] }] },
    { model: 'system.ai.gpt-6-1-terra', messages: [{ role: 'user', content: [{ type: 'text' }, 'not a dict'] }] },
    { model: 'system.ai.gpt-6-1-terra', messages: [{ role: 'user', content: [{ type: 'text' }] }] },
  ];
  for (const body of [...malformed, ...ineligible.map((value) => JSON.stringify(value))]) {
    expect(preserveGptCacheBoundary('/messages', body)).toBe(body);
  }

  const request = JSON.parse(userRequestBody('system.ai.gpt-6-1-terra', { type: 'ephemeral', ttl: '5m' }));
  for (const control of [null, 'present', 7, [], {}]) {
    const body = JSON.stringify({ ...request, cache_control: control });
    expect(preserveGptCacheBoundary('/messages', body)).toBe(body);
  }
  for (const number of ['9007199254740993', '-9007199254740993', '-0.0', '1e400']) {
    const body = `{"model":"system.ai.gpt-6-1-terra","metadata":{"value":${number}},"messages":[{"role":"user","content":[{"type":"text","text":"keep","cache_control":{"type":"ephemeral"}}]}]}`;
    expect(preserveGptCacheBoundary('/messages', body)).toBe(body);
  }
});

test('registered HTTP fetch hook rewrites only opted-in engine requests and preserves transport fields', async () => {
  type Request = { url: string; init?: { method?: string; body?: unknown; [key: string]: unknown }; [key: string]: unknown };
  type Next = ((event: Request) => unknown) & { origin: { plugin: string } };
  type FetchHook = (engine: { env: { get: (name: string) => Promise<unknown> } }, event: Request, next: Next) => Promise<unknown>;
  let httpFetch: unknown;
  register(((event: string, handler: unknown) => { if (event === 'http.fetch') httpFetch = handler; }) as unknown as On);
  const fetch = httpFetch as FetchHook;
  const control = { type: 'ephemeral', ttl: '5m' };
  const body = userRequestBody('system.ai.gpt-6-1-terra', control);
  const expected = JSON.parse(body);
  delete expected.messages[0].content[0].cache_control;
  expected.cache_control = control;
  const headers = { 'x-original': 'kept' };
  const event: Request = {
    url: 'https://gateway.example/v1/messages?beta=true',
    requestId: 'request-1',
    init: { method: 'post', body, headers, compress: true, redirect: 'manual', credentials: 'omit' },
  };
  const response = { status: 207, body: 'engine response' };
  const invoke = async (environment: Record<string, string | undefined>, request: Request, plugin = 'engine') => {
    const forwarded: Request[] = [];
    const next = Object.assign((value: Request) => { forwarded.push(value); return response; }, { origin: { plugin } }) as Next;
    return { forwarded, result: await fetch({ env: { get: async (name) => environment[name] } }, request, next) };
  };
  const env = { UG_CLAUDE_GPT_CACHE_BOUNDARY: '1', UG_CLAUDE_GPT_CACHE_BOUNDARY_BASE_URL: 'https://gateway.example' };
  const enabled = await invoke({ ...env, UG_CLAUDE_GPT_CACHE_BOUNDARY_BASE_URL: 'https://gateway.example///' }, event);
  expect(enabled.forwarded).toHaveLength(1);
  expect(enabled.forwarded[0]).not.toBe(event);
  expect(enabled.forwarded[0]).toEqual({ ...event, init: { ...event.init, body: JSON.stringify(expected) } });
  expect(enabled.forwarded[0].init?.headers).toBe(headers);
  expect(enabled.forwarded[0].init?.method).toBe('post');
  expect(enabled.forwarded[0].init?.compress).toBe(true);
  expect(enabled.result).toBe(response);

  const unchanged = [
    [env, event, 'other'],
    [{ UG_CLAUDE_GPT_CACHE_BOUNDARY_BASE_URL: env.UG_CLAUDE_GPT_CACHE_BOUNDARY_BASE_URL }, event],
    [{ ...env, UG_CLAUDE_GPT_CACHE_BOUNDARY: '0' }, event],
    [{ ...env, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1' }, event],
    [{ ...env, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '0' }, event],
    [{ ...env, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: 'true' }, event],
    [{ UG_CLAUDE_GPT_CACHE_BOUNDARY: '1' }, event],
    [{ ...env, UG_CLAUDE_GPT_CACHE_BOUNDARY_BASE_URL: 'https://elsewhere.example' }, event],
    [env, { ...event, init: { ...event.init, method: 'GET' } }],
    [env, { ...event, url: 'https://gateway.example/other' }],
    [env, { ...event, init: { ...event.init, body: new Uint8Array([1]) } }],
  ] as const;
  for (const [environment, request, plugin] of unchanged) {
    const result = await invoke(environment, request, plugin);
    expect(result.forwarded).toHaveLength(1);
    expect(result.forwarded[0]).toBe(request);
    expect(result.result).toBe(response);
  }
});
