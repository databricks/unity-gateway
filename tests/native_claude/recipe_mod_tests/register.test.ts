import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { RECIPE_FIELD } from '../hooks/constants.ts';

const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;
const CALLER_BODY = '{"caller_field":"keep"}';
const ROUTED_FLAGS = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
} as const;

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

test('a routed launch writes the recipe and preserves other request fields', async ($, on) => {
  const env = setup(on, {
    ...ROUTED_FLAGS,
    SMART_ROUTER_NAME: 'task_v3',
    CLAUDE_CODE_EXTRA_BODY: CALLER_BODY,
  });

  await $.session.start(start);

  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({
    caller_field: 'keep',
    [RECIPE_FIELD]: 'task_v3',
  });
});

test('a launch without a recipe of its own falls back to the stable recipe', async ($, on) => {
  const env = setup(on, { ...ROUTED_FLAGS, CLAUDE_CODE_EXTRA_BODY: CALLER_BODY });

  await $.session.start(start);

  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)[RECIPE_FIELD]).toBe('stable');
});

test('a session ug did not route keeps its request body', async ($, on) => {
  const env = setup(on, { SMART_ROUTER_NAME: 'task_v3', CLAUDE_CODE_EXTRA_BODY: CALLER_BODY });

  await $.session.start(start);

  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);
});

test('routing off from launch leaves a body without the recipe alone', async ($, on) => {
  const env = setup(on, {
    ENABLE_SMART_ROUTING_V2: '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0',
    SMART_ROUTER_NAME: 'task_v3',
    CLAUDE_CODE_EXTRA_BODY: CALLER_BODY,
  });

  await $.session.start(start);

  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);
});

test('routing off replaces a recipe that is already there', async ($, on) => {
  const env = setup(on, {
    ENABLE_SMART_ROUTING_V2: '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0',
    SMART_ROUTER_NAME: 'task_v3',
    CLAUDE_CODE_EXTRA_BODY: `{"caller_field":"keep","${RECIPE_FIELD}":"task_v3"}`,
  });

  await $.session.start(start);

  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({
    caller_field: 'keep',
    [RECIPE_FIELD]: 'DISABLED',
  });
});

test('a caller body that is not a JSON object is left alone', async ($, on) => {
  const env = setup(on, { ...ROUTED_FLAGS, CLAUDE_CODE_EXTRA_BODY: 'not json' });

  await $.session.start(start);

  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe('not json');
});

test('the session control file overrides the launch flags and turn steps reapply the recipe', async ($, on) => {
  const env = setup(on, {
    ...ROUTED_FLAGS,
    SMART_ROUTER_NAME: 'task_v3',
    // A session that routed before the control file turned routing off.
    CLAUDE_CODE_EXTRA_BODY: `{"caller_field":"keep","${RECIPE_FIELD}":"task_v3"}`,
    UCODE_SESSION_ENV_FILE: '/session/env.json',
  });
  let enabled = false;
  on('fs.read', () => ({ value: JSON.stringify({ ENABLE_SMART_ROUTING_SUBAGENT_ONLY: enabled ? '1' : '0' }) }));
  const event = { turnId: 'parent', index: 0, model: 'parent-model', messageCount: 1 } as const;
  const chunk = { kind: 'text', index: 0, text: 'Answer' } as const;
  const result = { turnId: event.turnId, index: event.index, answer: chunk.text, toolUses: [], stopReason: 'end_turn', usage: null } as const;
  on('turn.step', async function* ($, e) {
    yield chunk;
    return result;
  });

  await $.session.start(start);
  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)[RECIPE_FIELD]).toBe('DISABLED');

  for (const value of [true, false] as const) {
    enabled = value;
    // A settings reload resets the variable between turn steps.
    env.CLAUDE_CODE_EXTRA_BODY = `{"caller_field":"keep","${RECIPE_FIELD}":"stale"}`;
    const stream = $.turn.step(event);
    expect(await stream.next()).toEqual({ done: false, value: chunk });
    expect(await stream.next()).toEqual({ done: true, value: result });
    expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({
      caller_field: 'keep',
      [RECIPE_FIELD]: value ? 'task_v3' : 'DISABLED',
    });
  }
});
