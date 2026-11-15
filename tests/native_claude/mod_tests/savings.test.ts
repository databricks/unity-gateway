import type { On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';

const options = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
  ENABLE_SMART_ROUTING_SAVINGS: '1',
  orchestratorInstructions: 'Delegate using Agent.',
  savings_pricer: ['/python', '-m', 'ucode.smart_routing.savings'],
  savings_usage: '/session/usage.json',
  recipe: 'task_v3',
  unset: [],
};

test('a denied savings read preserves orchestration, startup and model requests', { options }, async ($, on) => {
  const env: Record<string, string | undefined> = { ...options } as any;
  on('env.get', (_, e) => {
    if (e.name === 'ENABLE_SMART_ROUTING_SAVINGS') return { deny: 'savings read denied' };
    return { value: env[e.name] };
  });
  on('env.set', (_, e) => {
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('session.start', () => ({ cwd: '/session' }));
  on('prompt.submit', (_, e) => ({ text: e.text }));
  on('tool.describe', (_, e) => ({ description: e.description }));
  on('turn.complete', () => ({ text: 'done' }));
  let requests = 0;
  on('turn.step', async function* (_, e) {
    requests += 1;
    return { turnId: e.turnId, index: e.index, answer: 'done', toolUses: [] };
  });

  let denied = false;
  try {
    await $.env.get('ENABLE_SMART_ROUTING_SAVINGS');
  } catch {
    denied = true;
  }
  expect(denied).toBe(true);
  expect(await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/session' }))
    .toEqual({ cwd: '/session' });
  expect((await $.tool.describe({ tool: 'Agent', description: 'Native Agent description.' })).description)
    .toContain(options.orchestratorInstructions);
  expect((await $.prompt.submit({ text: 'hello' })).text).toBe('hello');
  for (const agentId of [undefined, 'child']) {
    const result = await $.turn.step({
      turnId: 'turn', index: 0, messageCount: 1, model: 'claude-sonnet-4-6', agentId,
    }).next();
    expect(result.done).toBe(true);
    expect(result.value.answer).toBe('done');
  }
  expect(requests).toBe(2);
  expect((await $.turn.complete({ answer: 'done' })).text).toBe('done');
});

async function savingsRow($: any, on: On, incompleteUsage = false) {
  const state = {
    controls: {} as Record<string, string>, failWrites: false, writes: 0, prices: 0,
    usage: null as any,
    selectedModel: 'claude-opus-4-8',
    responseUsage: { input_tokens: 1, output_tokens: 1 } as any,
    priceBarrier: null as Promise<void> | null,
  };
  const clock = mock.clock(on);
  const env: Record<string, unknown> = { ...options, UCODE_SESSION_ENV_FILE: '/session/env.json' };
  on('env.get', (_, e) => ({ value: env[e.name] }));
  on('env.set', (_, e) => {
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('fs.read', (_, e) => ({ value: JSON.stringify(e.path === '/session/env.json' ? state.controls : {
    version: 1,
    start_model: 'claude-opus-4-8',
    first_main_model: 'claude-opus-4-8',
    main_model: 'claude-opus-4-8',
    first_prompt_seen: true,
    incomplete_usage: incompleteUsage,
    entries: [{
      agent: 'child', baseline: 'claude-opus-4-8', served: 'claude-sonnet-4-6',
      before_user_switch: true, usage: { input_tokens: 100, output_tokens: 10 },
    }],
  }) }));
  on('session.model', () => ({ value: state.selectedModel }));
  on('process.run', async (_, e) => {
    state.writes += 1;
    if (state.failWrites) return { value: { exitCode: 1, stderr: '', stdout: '{"savings":null}' } };
    state.usage = JSON.parse(e.init.stdin);
    state.prices += 1;
    if (state.priceBarrier) await state.priceBarrier;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      savings: { saved: true, amount: '$0.18', percent: '60%' },
    }) } };
  });
  on('session.start', () => ({ cwd: '/session' }));
  on('turn.complete', () => ({ text: 'done' }));
  on('turn.step', async function* (_, e) {
    return { turnId: e.turnId, index: e.index, answer: 'done', toolUses: [], usage: state.responseUsage };
  });
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['theirs'] }));
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/session' });
  const row = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} });
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn()).includes('$0.18')).toBe(!incompleteUsage);
  return { state, clock, row };
}

test('saved off/on controls hide and restore a mounted savings row', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  state.controls = { ENABLE_SMART_ROUTING_SAVINGS: '0' };
  await clock.advance(1000);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  state.controls = {};
  await clock.advance(1000);
  expect(JSON.stringify(await row.drawn())).toContain('$0.18');
});

test('a request model override keeps the selected main model and child baseline', { options }, async ($, on) => {
  const { state, clock } = await savingsRow($, on);
  await $.turn.step({ turnId: 'turn', index: 0, messageCount: 1, model: 'claude-haiku-4-5' }).next();
  await $.turn.step({
    turnId: 'child-turn', index: 0, messageCount: 1, model: 'claude-sonnet-4-6', agentId: 'child-2',
  }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.main_model).toBe('claude-opus-4-8');
  expect(state.usage.user_switched).toBe(false);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'child-2')).toEqual({
    agent: 'child-2', baseline: 'claude-opus-4-8', served: 'claude-sonnet-4-6',
    before_user_switch: true,
    usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
  });
});

test('a failed usage write removes the stale estimate until a later write succeeds', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  const priced = state.prices;
  state.failWrites = true;
  await $.turn.step({ turnId: 'turn', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.writes).toBe(2);
  expect(state.prices).toBe(priced);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');

  state.failWrites = false;
  await $.turn.complete({ answer: 'retry', isAborted: false });
  await clock.advance(1);
  expect(state.prices).toBe(priced + 1);
  expect(JSON.stringify(await row.drawn())).toContain('$0.18');
});


test('a background child observes a model switch before another parent request', { options }, async ($, on) => {
  const { state, clock } = await savingsRow($, on);
  state.selectedModel = 'claude-sonnet-4-6';
  await $.turn.step({
    turnId: 'child-turn', index: 1, messageCount: 2, model: 'claude-haiku-4-5', agentId: 'background-child',
  }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.main_model).toBe('claude-sonnet-4-6');
  expect(state.usage.user_switched).toBe(true);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'child').before_user_switch).toBe(true);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'background-child')).toEqual({
    agent: 'background-child', baseline: 'claude-sonnet-4-6', served: 'claude-haiku-4-5',
    before_user_switch: false,
    usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
  });
});

test('child steps retain a model switch while accounting is off', { options }, async ($, on) => {
  const { state, clock } = await savingsRow($, on);
  state.controls = { ENABLE_SMART_ROUTING_SAVINGS: '0' };
  state.selectedModel = 'claude-sonnet-4-6';
  await $.turn.step({
    turnId: 'off-turn', index: 0, messageCount: 1, model: 'claude-haiku-4-5', agentId: 'off-child',
  }).next();
  state.controls = {};
  state.selectedModel = 'claude-opus-4-8';
  await $.turn.step({
    turnId: 'on-turn', index: 0, messageCount: 1, model: 'claude-sonnet-4-6', agentId: 'on-child',
  }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.user_switched).toBe(true);
  expect(state.usage.entries.some((entry: any) => entry.agent === 'off-child')).toBe(false);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'on-child').before_user_switch).toBe(false);
});

for (const agentId of [undefined, 'child-without-usage']) {
  test(`missing ${agentId ? 'child' : 'main'} usage keeps the estimate hidden after later flushes`, { options }, async ($, on) => {
    const { state, clock, row } = await savingsRow($, on);
    state.responseUsage = undefined;
    await $.turn.step({ turnId: 'missing', index: 0, messageCount: 1, model: 'claude-sonnet-4-6', agentId }).next();
    await clock.advance(1);
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.usage.incomplete_usage).toBe(true);
    state.responseUsage = { input_tokens: 1, output_tokens: 1 };
    await $.turn.step({ turnId: 'later', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.usage.incomplete_usage).toBe(true);
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
    state.controls = { ENABLE_SMART_ROUTING_SAVINGS: '0' };
    await clock.advance(1000);
    state.controls = {};
    await clock.advance(1000);
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  });
}

test('an incomplete saved snapshot remains hidden after module initialization', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on, true);
  await $.turn.step({ turnId: 'later', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.incomplete_usage).toBe(true);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
});

test('untracked missing usage does not invalidate an enabled estimate', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  state.controls = { ENABLE_SMART_ROUTING_SAVINGS: '0' };
  state.responseUsage = undefined;
  await $.turn.step({ turnId: 'off', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  state.controls = {};
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.incomplete_usage === true).toBe(false);
  expect(JSON.stringify(await row.drawn())).toContain('$0.18');
});

test('an in-flight price result cannot restore an estimate after missing usage', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  let release!: () => void;
  state.priceBarrier = new Promise<void>(resolve => { release = resolve; });
  await $.turn.step({ turnId: 'priced', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.writes).toBe(2);
  state.responseUsage = undefined;
  await $.turn.step({ turnId: 'missing', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  release();
  state.priceBarrier = null;
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.incomplete_usage).toBe(true);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
});
