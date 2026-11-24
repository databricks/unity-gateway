import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { RECIPE_FIELD } from '../hooks/constants.ts';

const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;
const body = '{"caller_field":"keep"}';
const routed = { ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' };

function setup(on: On, env: Record<string, string | undefined>) {
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => { env[e.name] = e.value; return { value: undefined }; });
  on('session.start', ($, e) => ({ cwd: e.cwd }));
  return env;
}

test('a non-ug launch keeps its request body', async ($, on) => {
  const env = setup(on, { SMART_ROUTER_NAME: 'task_v3', CLAUDE_CODE_EXTRA_BODY: body });
  await $.session.start(start);
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(body);
});

test('a never-routed body does not gain a disabled recipe', async ($, on) => {
  const env = setup(on, { ...routed, ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0', CLAUDE_CODE_EXTRA_BODY: body });
  await $.session.start(start);
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(body);
});

test('invalid and non-object bodies are left alone', async ($, on) => {
  const env = setup(on, { ...routed });
  for (const value of ['not json', '[]', 'null']) {
    env.CLAUDE_CODE_EXTRA_BODY = value;
    await $.session.start(start);
    expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(value);
  }
});

test('the default recipe preserves caller fields', async ($, on) => {
  const env = setup(on, { ...routed, CLAUDE_CODE_EXTRA_BODY: body });
  await $.session.start(start);
  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({ caller_field: 'keep', [RECIPE_FIELD]: 'stable' });
});

test('session controls override the launch flags', async ($, on) => {
  const env = setup(on, {
    ...routed, SMART_ROUTER_NAME: 'task_v3', UCODE_SESSION_ENV_FILE: '/session/env.json',
    CLAUDE_CODE_EXTRA_BODY: `{"caller_field":"keep","${RECIPE_FIELD}":"task_v3"}`,
  });
  let enabled = false;
  on('fs.read', () => ({ value: JSON.stringify({ ENABLE_SMART_ROUTING_SUBAGENT_ONLY: enabled ? '1' : '0' }) }));
  const event = { turnId: 'parent', index: 0, model: 'parent-model', messageCount: 1 } as const;
  const chunk = { kind: 'text', index: 0, text: 'Answer' } as const;
  const result = { turnId: event.turnId, index: event.index, answer: chunk.text, toolUses: [], stopReason: 'end_turn', usage: null } as const;
  on('turn.step', async function* () { yield chunk; return result; });
  await $.session.start(start);
  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({ caller_field: 'keep', [RECIPE_FIELD]: 'DISABLED' });
  for (const value of [true, false] as const) {
    enabled = value;
    // Settings can reload between steps and restore a stale body.
    env.CLAUDE_CODE_EXTRA_BODY = `{"caller_field":"keep","${RECIPE_FIELD}":"stale"}`;
    const stream = $.turn.step(event);
    expect(await stream.next()).toEqual({ done: false, value: chunk });
    expect(await stream.next()).toEqual({ done: true, value: result });
    expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({
      caller_field: 'keep', [RECIPE_FIELD]: value ? 'task_v3' : 'DISABLED',
    });
  }
});
