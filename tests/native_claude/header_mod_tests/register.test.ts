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


test('shared context follows the effective language and preserves other context blocks', async ($, on) => {
  let language: unknown = 'English';
  on('settings.read', () => ({ value: { language } }));
  const event = { blocks: [{ name: 'existing', text: 'Existing context' }] };
  let calls = 0;
  on('prompt.context', ($, e) => {
    calls += 1;
    expect(e.blocks).toEqual(event.blocks);
    return { blocks: e.blocks };
  });
  for (const value of ['English', '中文', 'Português', '', undefined]) {
    language = value;
    const result = await $.prompt.context(event);
    expect(result.blocks).toHaveLength(2);
    expect(result.blocks[0]).toEqual(event.blocks[0]);
    expect(result.blocks[1].name).toBe('responseLanguage');
    const instruction = result.blocks[1].text;
    if (value) expect(instruction).toContain(`Always respond in ${value}.`);
    else expect(instruction).toContain('language of the original task assigned to you');
    expect(instruction).toContain('requested target language for translations');
    expect(instruction).toContain('quoted source text');
    expect(instruction).toContain('progress summaries');
  }
  expect(calls).toBe(5);
});


test('submitted prompts refresh language context without changing the user text', async ($, on) => {
  let language: unknown = 'English';
  let invalidated = false;
  on('settings.read', () => ({ value: { language } }));
  on('ui.invalidate', ($, e) => {
    expect(e.event).toBe('prompt.context');
    invalidated = true;
    return { value: undefined };
  });
  on('prompt.submit', ($, e) => {
    expect(invalidated).toBe(true);
    expect(e.text).toBe('Original task');
    expect(e.context?.[0]).toBe('Existing context');
    return { text: e.text, context: e.context };
  });
  for (const value of ['English', '中文', undefined]) {
    language = value;
    invalidated = false;
    const result = await $.prompt.submit({ text: 'Original task', context: ['Existing context'] });
    expect(result.text).toBe('Original task');
    expect(result.context).toHaveLength(2);
    expect(result.context?.[1]).toContain(value ? `Always respond in ${value}.` : 'language of the original task assigned to you');
  }
});
