import { expect, test } from 'claude-code/testing';

test('session start forwards the event and result', async ($, on) => {
  const event = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;
  let calls = 0;
  on('session.start', ($, e) => {
    calls += 1;
    expect(e).toMatchObject(event);
    return { cwd: '/resolved-work' };
  });

  const result = await $.session.start(event);
  expect(calls).toBe(1);
  expect(result).toEqual({ cwd: '/resolved-work' });
});

const routed = {
  ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1', unset: [], recipe: 'custom "quoted" λ',
};
const start = {surface: 'terminal', isInteractive: true, cwd: '/work'} as const;

function environment(on, flags, body = '{"caller":1}', denied = '') {
  const env = {...flags, CLAUDE_CODE_EXTRA_BODY: body};
  on('env.get', ($, e) => ({value: env[e.name]}));
  on('env.set', ($, e) => {
    if (e.name === denied) return {deny: 'Updates denied'};
    env[e.name] = e.value;
    return {value: undefined};
  });
  on('session.start', () => ({cwd: '/work'}));
  return env;
}

test('complete toggle restores baseline and preserves new caller metadata', {options: routed}, async ($, on) => {
  const flags = {ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', ENABLE_SMART_ROUTER_ORCHESTRATOR: '1'};
  const env = environment(on, flags);
  await $.session.start(start);
  for (const action of ['off', 'off', 'on', 'on']) {
    env.CLAUDE_CODE_EXTRA_BODY = '{"caller":2}';
    const reply = await $.command.run({command: 'smart-router', args: action});
    expect(reply.text).toContain(`Smart Router is ${action}`);
    expect(env).toEqual({
      ...(action === 'on' ? flags : {ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0', ENABLE_SMART_ROUTER_ORCHESTRATOR: '0'}),
      CLAUDE_CODE_EXTRA_BODY: JSON.stringify({caller: 2, smart_router_recipe_name: action === 'on' ? routed.recipe : 'DISABLED'}),
    });
  }
});

test('empty and absent launch flags are restored exactly', {options: {
  ENABLE_SMART_ROUTING_V2: '1', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '', unset: ['ENABLE_SMART_ROUTER_ORCHESTRATOR'], recipe: 'task_v3',
}}, async ($, on) => {
  const env = environment(on, {ENABLE_SMART_ROUTING_V2: '1', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '', ENABLE_SMART_ROUTER_ORCHESTRATOR: undefined});
  await $.session.start(start);
  const before = {...env};
  await $.command.run({command: 'smart-router', args: 'off'});
  await $.session.start(start);
  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY).smart_router_recipe_name).toBe('DISABLED');
  await $.command.run({command: 'smart-router', args: 'on'});
  expect(env).toEqual(before);
});

test('invalid input and denied writes preserve prior environment', {options: routed}, async ($, on) => {
  const flags = {ENABLE_SMART_ROUTING_V2: '1', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', ENABLE_SMART_ROUTER_ORCHESTRATOR: '1'};
  const env = environment(on, flags, '{}', 'ENABLE_SMART_ROUTER_ORCHESTRATOR');
  const before = {...env};
  const reply = await $.command.run({command: 'smart-router', args: 'off'});
  expect(reply.text).toContain('Could not update');
  expect(env).toEqual(before);
  await $.command.run({command: 'smart-router', args: 'typo'});
  expect(env).toEqual(before);
  for (const raw of ['broken', 'null', '[]', 'true']) {
    env.CLAUDE_CODE_EXTRA_BODY = raw;
    const result = await $.command.run({command: 'smart-router', args: 'off'});
    expect(result.text).toContain('Could not update');
    expect(env).toEqual({...before, CLAUDE_CODE_EXTRA_BODY: raw});
  }
});

test('orchestration alone does not activate routing', {options: {ENABLE_SMART_ROUTER_ORCHESTRATOR: '1'}}, async ($, on) => {
  const env = environment(on, {ENABLE_SMART_ROUTER_ORCHESTRATOR: '1'});
  const before = {...env};
  await $.session.start(start);
  const result = await $.command.run({command: 'smart-router', args: 'on'});
  expect(result.text).toContain('not enabled');
  expect(env).toEqual(before);
});
