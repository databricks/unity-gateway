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
  routeCommand: ['/router'],
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

async function savingsRow($: any, on: On, incompleteUsage = false, legacyChild = false) {
  const state = {
    controls: {} as Record<string, string>, failWrites: false, writes: 0, prices: 0,
    usage: null as any,
    selectedModel: 'claude-opus-4-8',
    responseUsage: { input_tokens: 1, output_tokens: 1 } as any,
    priceBarrier: null as Promise<void> | null,
    stepBarrier: null as Promise<void> | null,
    stepStarted: null as (() => void) | null,
    controlReads: 0, showEstimate: true, validForMs: 24 * 60 * 60 * 1000,
    routeModel: null as string | null, routeExitCode: 0,
    stepError: null as Error | null, emitPartial: false, responseAnswer: 'done',
  };
  const clock = mock.clock(on);
  const env: Record<string, unknown> = { ...options, UCODE_SESSION_ENV_FILE: '/session/env.json' };
  on('env.get', (_, e) => ({ value: env[e.name] }));
  on('env.set', (_, e) => {
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('fs.read', (_, e) => {
    if (e.path === '/session/env.json') state.controlReads += 1;
    return { value: JSON.stringify(e.path === '/session/env.json' ? state.controls : {
    version: 1,
    start_model: 'claude-opus-4-8',
    first_main_model: 'claude-opus-4-8',
    main_model: 'claude-opus-4-8',
    first_prompt_seen: true,
    incomplete_usage: incompleteUsage,
    entries: [{
      agent: 'child', baseline: 'claude-opus-4-8', served: 'claude-sonnet-4-6',
      before_user_switch: true, routed: legacyChild ? undefined : true,
      usage: { input_tokens: 100, output_tokens: 10 },
    }],
  }) };
  });
  on('session.end', () => ({ sessionId: 'session' }));
  on('session.model', () => ({ value: state.selectedModel }));
  on('session.id', () => ({ value: 'session' }));
  on('tool.call', () => ({ result: 'ok' }));
  on('agent.spawn', (_, e) => ({ agentId: e.tool_use_id, model: e.model ?? e.parentModel }));
  on('fs.write', () => ({ value: undefined }));
  on('ui.log', () => ({ value: undefined }));
  on('process.run', async (_, e) => {
    const input = JSON.parse(e.init.stdin);
    if ('tool_input' in input) {
      return { value: { exitCode: state.routeExitCode, stderr: '', stdout: state.routeModel
        ? JSON.stringify({ model: state.routeModel, rationale: 'Fixture route' }) : '' } };
    }
    state.writes += 1;
    if (state.failWrites) return { value: { exitCode: 1, stderr: '', stdout: '{"savings":null}' } };
    state.usage = JSON.parse(e.init.stdin);
    state.prices += 1;
    if (state.priceBarrier) await state.priceBarrier;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      savings: state.showEstimate ? { saved: true, amount: '$0.18', percent: '60%' } : null,
      valid_for_ms: state.validForMs,
    }) } };
  });
  on('session.start', () => ({ cwd: '/session' }));
  on('turn.complete', () => ({ text: 'done' }));
  on('turn.step', async function* (_, e) {
    state.stepStarted?.();
    if (state.stepBarrier) await state.stepBarrier;
    if (state.emitPartial) yield { kind: 'text', index: 0, text: 'partial' };
    if (state.stepError) throw state.stepError;
    return { turnId: e.turnId, index: e.index, answer: state.responseAnswer, toolUses: [], usage: state.responseUsage };
  });
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['theirs'] }));
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/session' });
  const row = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} });
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn()).includes('$0.18')).toBe(!incompleteUsage && !legacyChild);
  return { state, clock, row };
}

async function spawnChild($: any, id: string, parent: string, model?: string) {
  await $.tool.call({
    tool: 'Agent', tool_use_id: id, prompt: 'Inspect the parser.',
    description: 'Inspect parser', subagent_type: 'general-purpose',
    ...(model ? { model } : {}),
  });
  return $.agent.spawn({
    tool_use_id: id, prompt: 'Inspect the parser.', description: 'Inspect parser',
    subagentType: 'general-purpose', parentModel: parent, model,
    provider: { plugin: 'engine', tier: 'core' }, background: true, fork: false,
    name: id, permissionMode: 'default', parentAgentId: 'main', cwd: '/session',
  });
}

test('saved off/on controls restore a mounted savings row only while its rates are valid', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  state.controls = { ENABLE_SMART_ROUTING_SAVINGS: '0' };
  await clock.advance(1000);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  state.controls = {};
  await clock.advance(1000);
  expect(JSON.stringify(await row.drawn())).toContain('$0.18');

  state.validForMs = 1000;
  await $.turn.step({ turnId: 'expiring', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn())).toContain('$0.18');
  const usage = JSON.stringify(state.usage);
  const prices = state.prices;
  await clock.advance(2000);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  expect(JSON.stringify(state.usage)).toBe(usage);
  expect(state.prices).toBe(prices);
  const reads = state.controlReads;
  await clock.advance(3000);
  expect(state.controlReads).toBe(reads);

  state.validForMs = 10000;
  await $.turn.step({ turnId: 'fresh', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn())).toContain('$0.18');
  expect(state.usage.entries.find((entry: any) => entry.agent === 'main').usage.input_tokens).toBe(2);
});

test('a delayed price result cannot display an estimate past its validity', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  state.validForMs = 1000;
  let release!: () => void;
  state.priceBarrier = new Promise<void>(resolve => { release = resolve; });
  await $.turn.step({ turnId: 'delayed', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.writes).toBe(2);
  const usage = JSON.stringify(state.usage);
  await clock.advance(2000);
  release();
  state.priceBarrier = null;
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  expect(JSON.stringify(state.usage)).toBe(usage);
});

test('a request model override keeps the selected main model and child baseline', { options }, async ($, on) => {
  const { state, clock } = await savingsRow($, on);
  await $.turn.step({ turnId: 'turn', index: 0, messageCount: 1, model: 'claude-haiku-4-5' }).next();
  state.routeModel = 'claude-sonnet-4-6';
  await spawnChild($, 'child-2', state.selectedModel);
  await $.turn.step({
    turnId: 'child-turn', index: 0, messageCount: 1, model: 'claude-sonnet-4-6', agentId: 'child-2',
  }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.main_model).toBe('claude-opus-4-8');
  expect(state.usage.user_switched).toBe(false);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'child-2')).toEqual({
    agent: 'child-2', baseline: 'claude-opus-4-8', served: 'claude-sonnet-4-6',
    before_user_switch: true, routed: true,
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
  state.routeModel = 'claude-haiku-4-5';
  await spawnChild($, 'background-child', state.selectedModel);
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
    before_user_switch: false, routed: true,
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


test('savings refresh stops without an estimate and restarts after pricing recovers', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  state.showEstimate = false;
  await $.turn.step({ turnId: 'no-price', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  const reads = state.controlReads;
  await clock.advance(3000);
  expect(state.controlReads).toBe(reads);

  state.showEstimate = true;
  await $.turn.step({ turnId: 'priced', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn())).toContain('$0.18');
  state.controls = { ENABLE_SMART_ROUTING_SAVINGS: '0' };
  await clock.advance(1000);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
});

test('session end cancels savings refresh', { options }, async ($, on) => {
  const { state, clock } = await savingsRow($, on);
  await $.session.end({ reason: 'prompt_input_exit' });
  await clock.advance(1);
  const reads = state.controlReads;
  await clock.advance(3000);
  expect(state.controlReads).toBe(reads);
});

test('a saved off toggle during a request prevents later accounting', { options }, async ($, on) => {
  const { state, clock } = await savingsRow($, on);
  let release!: () => void;
  const started = new Promise<void>(resolve => { state.stepStarted = resolve; });
  state.stepBarrier = new Promise<void>(resolve => { release = resolve; });
  const request = $.turn.step({
    turnId: 'in-flight', index: 0, messageCount: 1, model: 'claude-sonnet-4-6', agentId: 'off-child',
  }).next();
  await started;
  state.controls = { ENABLE_SMART_ROUTING_SAVINGS: '0' };
  release();
  await request;
  state.stepBarrier = null;
  state.controls = {};
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.entries.some((entry: any) => entry.agent === 'off-child')).toBe(false);
});

for (const failed of [false, true]) {
  test('a ' + (failed ? 'failed' : 'empty') + ' route keeps pinned child usage without routing credit', { options }, async ($, on) => {
    const { state, clock } = await savingsRow($, on);
    state.routeExitCode = failed ? 1 : 0;
    const child = await spawnChild($, 'pinned-child', state.selectedModel, 'claude-haiku-4-5');
    expect(child.model).toBe('claude-haiku-4-5');
    await $.turn.step({
      turnId: 'pinned', index: 0, messageCount: 1, model: child.model, agentId: child.agentId,
    }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.usage.entries.find((entry: any) => entry.agent === child.agentId)).toEqual({
      agent: 'pinned-child', baseline: 'claude-haiku-4-5', served: 'claude-haiku-4-5',
      before_user_switch: true, routed: false,
      usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    });
  });
}

test('legacy child usage without routing attribution is retained and hides the estimate', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on, false, true);
  await $.turn.step({ turnId: 'later', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.incomplete_usage).toBe(true);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'child').usage.input_tokens).toBe(100);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
});

for (const agentId of [undefined, 'interrupted-child']) {
  for (const name of ['Error', 'AbortError']) {
    test(name + ' on a ' + (agentId ? 'child' : 'main') + ' step preserves the error and hides unknown usage', { options }, async ($, on) => {
      const { state, clock, row } = await savingsRow($, on);
      state.stepError = new Error('Request interrupted');
      state.stepError.name = name;
      // Native converts a failed hook into a HooksError. Savings must preserve that failure.
      state.controls = { ENABLE_SMART_ROUTING_SAVINGS: '0' };
      let controlError: any;
      try {
        await $.turn.step({ turnId: 'control', index: 0, messageCount: 1, model: 'claude-sonnet-4-6', agentId }).next();
      } catch (caught) {
        controlError = caught;
      }
      expect(String(controlError)).toContain('no implementation for turn.step');
      expect(state.usage.incomplete_usage).toBe(false);
      state.controls = {};
      let error: any;
      try {
        await $.turn.step({ turnId: 'failed', index: 0, messageCount: 1, model: 'claude-sonnet-4-6', agentId }).next();
      } catch (caught) {
        error = caught;
      }
      expect(String(error)).toBe(String(controlError));
      await $.turn.complete({ answer: '', isAborted: true });
      await clock.advance(1);
      expect(state.usage.incomplete_usage).toBe(true);
      expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
      state.stepError = null;
      await $.turn.step({ turnId: 'later', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
      await $.turn.complete({ answer: 'done', isAborted: false });
      await clock.advance(1);
      expect(state.usage.incomplete_usage).toBe(true);
      expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
    });
  }
}

test('cancelling a streamed step without final usage hides the estimate', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  state.emitPartial = true;
  const step = $.turn.step({ turnId: 'cancelled', index: 0, messageCount: 1, model: 'claude-opus-4-8' });
  expect((await step.next()).value).toEqual({ kind: 'text', index: 0, text: 'partial' });
  await step.return(undefined);
  await $.turn.complete({ answer: '', isAborted: true });
  await clock.advance(1);
  expect(state.usage.incomplete_usage).toBe(true);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
});

for (const measured of [false, true]) {
  test('an empty aborted response with ' + (measured ? 'explicit zero' : 'absent') + ' usage preserves accounting uncertainty', { options }, async ($, on) => {
    const { state, clock, row } = await savingsRow($, on);
    state.responseAnswer = '';
    state.responseUsage = measured ? { input_tokens: 0, output_tokens: 0 } : undefined;
    await $.turn.step({ turnId: 'aborted', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
    await $.turn.complete({ answer: '', isAborted: true });
    await clock.advance(1);
    expect(state.usage.incomplete_usage).toBe(!measured);
    expect(JSON.stringify(await row.drawn()).includes('$0.18')).toBe(measured);
  });
}
