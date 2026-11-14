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
  on('settings.read', () => ({ value: { language } }));
  const blocks = [{ name: 'existing', text: 'Existing context' }];
  let calls = 0;
  on('prompt.context', ($, e) => {
    calls += 1;
    expect(e.blocks).toEqual(blocks);
    return { blocks: e.blocks };
  });
  await $.session.start(start);
  for (const value of ['English', '中文', 'Português', '', undefined]) {
    language = value;
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
  }
  expect(calls).toBe(5);
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
