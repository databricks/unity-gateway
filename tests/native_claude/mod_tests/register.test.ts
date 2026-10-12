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

function environment(on, flags, headers = 'X-Caller: keep', denied = '') {
  const env = {...flags, ANTHROPIC_CUSTOM_HEADERS: headers, CLAUDE_CODE_EXTRA_BODY: '{"caller":1}'};
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
    env.ANTHROPIC_CUSTOM_HEADERS = 'X-Caller: updated\ndatabricks-smart-router-recipe: old\nDATABRICKS-SMART-ROUTER-RECIPE: duplicate\nX-Other: keep';
    const reply = await $.command.run({command: 'smart-router', args: action});
    expect(reply.text).toContain(`Smart Router is ${action}`);
    expect(env).toEqual({
      ...(action === 'on' ? flags : {ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0', ENABLE_SMART_ROUTER_ORCHESTRATOR: '0'}),
      ANTHROPIC_CUSTOM_HEADERS: `X-Caller: updated\nX-Other: keep\nDatabricks-Smart-Router-Recipe: ${action === 'on' ? routed.recipe : 'DISABLED'}`,
      CLAUDE_CODE_EXTRA_BODY: '{"caller":2}',
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
  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Caller: keep\nDatabricks-Smart-Router-Recipe: DISABLED');
  await $.command.run({command: 'smart-router', args: 'on'});
  expect(env).toEqual(before);
});

test('invalid arguments and denied header writes preserve prior environment', {options: routed}, async ($, on) => {
  const flags = {ENABLE_SMART_ROUTING_V2: '1', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', ENABLE_SMART_ROUTER_ORCHESTRATOR: '1'};
  const env = environment(on, flags, 'X-Caller: keep', 'ANTHROPIC_CUSTOM_HEADERS');
  const before = {...env};
  const reply = await $.command.run({command: 'smart-router', args: 'off'});
  expect(reply.text).toContain('Could not update');
  expect(env).toEqual(before);
  await $.command.run({command: 'smart-router', args: 'typo'});
  expect(env).toEqual(before);
});

test('orchestration alone does not activate routing', {options: {ENABLE_SMART_ROUTER_ORCHESTRATOR: '1'}}, async ($, on) => {
  const env = environment(on, {ENABLE_SMART_ROUTER_ORCHESTRATOR: '1'});
  const before = {...env};
  await $.session.start(start);
  const result = await $.command.run({command: 'smart-router', args: 'on'});
  expect(result.text).toContain('not enabled');
  expect(env).toEqual(before);
});

test('absent and empty headers work without parsing caller bodies', {options: routed}, async ($, on) => {
  const env = environment(on, {});
  for (const headers of [undefined, '']) {
    for (const raw of ['broken', 'null', '[]', 'true', '{"smart_router_recipe_name":"caller_override"}']) {
      env.ANTHROPIC_CUSTOM_HEADERS = headers;
      env.CLAUDE_CODE_EXTRA_BODY = raw;
      const reply = await $.command.run({command: 'smart-router', args: 'on'});
      expect(reply.text).toContain('Smart Router is on');
      expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe(`Databricks-Smart-Router-Recipe: ${routed.recipe}`);
      expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(raw);
    }
  }
});

test('recipe line breaks are rejected before environment writes', {options: {
  ...routed, recipe: 'task_v3\r\nX-Injected: value',
}}, async ($, on) => {
  const env = environment(on, {});
  const before = {...env};
  const reply = await $.command.run({command: 'smart-router', args: 'on'});
  expect(reply.text).toContain('Could not update');
  expect(env).toEqual(before);
});
