import type { On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';

const options = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
  ENABLE_SMART_ROUTING_SAVINGS: '1',
  orchestratorInstructions: 'Delegate using Agent.',
  savings_pricer: ['/python', '-m', 'ucode.smart_routing.savings'],
  savings_usage_dir: '/session/usage',
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

type SavedUsageFile = 'missing' | 'unreadable' | 'malformed' | 'unsupported' | 'invalid_entry';
type SavingsFixture = {
  incompleteUsage?: boolean;
  legacyChild?: boolean;
  legacySession?: boolean;
  usageFile?: SavedUsageFile;
  invalidSavedUsage?: Record<string, unknown>;
};

async function savingsRow($: any, on: On, fixture: SavingsFixture = {}) {
  const { incompleteUsage = false, legacyChild = false, legacySession = false, usageFile, invalidSavedUsage } = fixture;
  const state = {
    controls: {} as Record<string, string>, failWrites: false, writes: 0, prices: 0,
    usage: null as any, sessionId: 'session',
    denySavingsWrites: false, failControls: false, denyEnvironmentRead: false,
    selectedModel: 'claude-opus-4-8',
    responseUsage: { input_tokens: 1, output_tokens: 1 } as any,
    priceBarrier: null as Promise<void> | null,
    writeBarrier: null as Promise<void> | null,
    stepBarrier: null as Promise<void> | null,
    stepStarted: null as (() => void) | null,
    controlReads: 0, savingsFlagReads: 0, failModelRead: false, showEstimate: true, validForMs: 24 * 60 * 60 * 1000,
    routeModel: null as string | null, routeExitCode: 0,
    stepError: null as Error | null, emitPartial: false, responseAnswer: 'done',
    saved: true, requestModel: undefined as string | undefined,
  };
  const files = new Map<string, string>();
  const initialPath = `${options.savings_usage_dir}/session.json`;
  const clock = mock.clock(on);
  const env: Record<string, unknown> = { ...options, UCODE_SESSION_ENV_FILE: '/session/env.json', UCODE_SMART_ROUTER_PYTHON: '/python' };
  on('env.get', (_, e) => {
    if (state.denyEnvironmentRead && e.name === 'ENABLE_SMART_ROUTING_V2') return { deny: 'environment unavailable' };
    if (e.name === 'ENABLE_SMART_ROUTING_SAVINGS') state.savingsFlagReads += 1;
    return { value: env[e.name] };
  });
  on('env.set', (_, e) => {
    if (state.denySavingsWrites && e.name === 'ENABLE_SMART_ROUTING_SAVINGS') return { deny: 'savings write denied' };
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('fs.exists', (_, e) => ({ value: files.has(e.path) || (e.path === initialPath && usageFile !== 'missing') }));
  on('fs.read', (_, e) => {
    if (e.path === '/session/env.json') {
      state.controlReads += 1;
      return { value: JSON.stringify(state.controls) };
    }
    if (files.has(e.path)) return { value: files.get(e.path)! };
    if (e.path === initialPath) {
      if (usageFile === 'unreadable' || usageFile === 'missing') return { deny: 'usage file unavailable' };
      if (usageFile === 'malformed') return { value: '{"version":1,"entries":[' };
    }
    return { value: JSON.stringify({
    version: usageFile === 'unsupported' ? 99 : 1,
    session_id: legacySession ? undefined : 'session',
    start_model: 'claude-opus-4-8',
    first_main_model: 'claude-opus-4-8',
    main_model: 'claude-opus-4-8',
    first_prompt_seen: true,
    incomplete_usage: incompleteUsage,
    entries: [{
      agent: 'child', baseline: 'claude-opus-4-8', served: 'claude-sonnet-4-6',
      before_user_switch: true, routed: legacyChild ? undefined : true,
      usage: invalidSavedUsage ?? { input_tokens: usageFile === 'invalid_entry' ? 'unknown' : 100, output_tokens: 10 },
    }],
  }) };
  });
  on('session.end', () => ({ sessionId: state.sessionId }));
  on('session.model', () => state.failModelRead
    ? { deny: 'model lookup unavailable' } : { value: state.selectedModel });
  on('session.id', () => ({ value: state.sessionId }));
  on('tool.call', () => ({ result: 'ok' }));
  on('agent.spawn', (_, e) => ({ agentId: e.tool_use_id, model: e.model ?? e.parentModel }));
  on('fs.write', () => ({ value: undefined }));
  on('ui.log', () => ({ value: undefined }));
  async function priceResult(usage: any) {
    state.prices += 1;
    if (state.priceBarrier) await state.priceBarrier;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      savings: state.showEstimate && usage.entries.length ? { saved: state.saved, amount: '$0.18', percent: '60%' } : null,
      valid_for_ms: state.validForMs,
    }) } };
  }
  on('process.run', async (_, e) => {
    if (!e.init?.stdin && e.argv.includes('--usage')) {
      return priceResult(JSON.parse(files.get(e.argv[e.argv.indexOf('--usage') + 1])!));
    }
    // Session controls are an external CLI boundary; the mod must survive its failure.
    if (!e.init?.stdin) {
      if (!state.failControls) state.controls.ENABLE_SMART_ROUTING_SAVINGS = String(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY);
      return { value: { exitCode: state.failControls ? 1 : 0, stdout: '', stderr: '' } };
    }
    const input = JSON.parse(e.init.stdin);
    if ('tool_input' in input) {
      return { value: { exitCode: state.routeExitCode, stderr: '', stdout: state.routeModel
        ? JSON.stringify({ model: state.routeModel, rationale: 'Fixture route' }) : '' } };
    }
    state.writes += 1;
    if (state.failWrites) return { value: { exitCode: 1, stderr: '', stdout: '{"savings":null}' } };
    if (state.writeBarrier) await state.writeBarrier;
    state.usage = JSON.parse(e.init.stdin);
    files.set(e.argv[e.argv.indexOf('--usage') + 1], e.init.stdin);
    return e.argv.includes('--no-price')
      ? { value: { exitCode: 0, stderr: '', stdout: '{"savings":null}' } }
      : priceResult(state.usage);
  });
  on('session.start', () => ({ cwd: '/session' }));
  on('prompt.submit', (_, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: 'done' }));
  on('turn.step', async function* (_, e) {
    state.requestModel = e.model;
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
  expect(JSON.stringify(await row.drawn()).includes('$0.18')).toBe(!incompleteUsage && !legacyChild && !legacySession && usageFile === undefined && invalidSavedUsage === undefined);
  return { state, clock, row, env, files };
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


test('a pending price cannot restore an estimate after a newer save fails', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  let release!: () => void;
  state.priceBarrier = new Promise<void>(resolve => { release = resolve; });
  try {
    await $.turn.step({ turnId: 'pending', index: 0, messageCount: 1, model: state.selectedModel }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    state.failWrites = true;
    await $.turn.step({ turnId: 'failed', index: 0, messageCount: 1, model: state.selectedModel }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  } finally {
    state.priceBarrier = null;
    release();
  }
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  state.failWrites = false;
  await $.turn.complete({ answer: 'retry', isAborted: false });
  await clock.advance(1);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'main').usage.input_tokens).toBe(2);
  expect(JSON.stringify(await row.drawn())).toContain('$0.18');
});

test('a background child observes a model switch before another parent request' , { options }, async ($, on) => {
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

for (const agentId of [undefined, 'background-child']) {
  test(`a failed model lookup after a switch hides ${agentId ? 'child' : 'main'} savings`, { options }, async ($, on) => {
    const { state, clock, row } = await savingsRow($, on);
    state.routeModel = 'claude-haiku-4-5';
    if (agentId) await spawnChild($, agentId, state.selectedModel);
    state.selectedModel = 'claude-sonnet-4-6';
    state.failModelRead = true;
    const result = await $.turn.step({
      turnId: 'unknown-selection', index: 0, messageCount: 1, model: 'claude-haiku-4-5', agentId,
    }).next();
    expect(result.value.answer).toBe('done');
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.usage.incomplete_usage).toBe(true);
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');

    state.failModelRead = false;
    await $.turn.step({
      turnId: 'known-selection', index: 0, messageCount: 1, model: 'claude-haiku-4-5', agentId,
    }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.usage.incomplete_usage).toBe(true);
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
    expect(state.usage.entries.find((entry: any) => entry.agent === 'child').usage.input_tokens).toBe(100);
  });
}

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
  const { state, clock, row } = await savingsRow($, on, { incompleteUsage: true });
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
  const { state, clock, row } = await savingsRow($, on, { legacyChild: true });
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


test('denied savings writes preserve native toggles and rollback', { options }, async ($, on) => {
  const { state, env } = await savingsRow($, on);
  state.denySavingsWrites = true;
  for (const action of ['off', 'on']) {
    expect((await $.command.run({ command: 'smart-router', args: action })).text).toBe(`Smart Router is ${action}`);
    expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe(action === 'on' ? '1' : '0');
    expect(env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe(action === 'on' ? '1' : '0');
  }
  const body = env.CLAUDE_CODE_EXTRA_BODY;
  state.failControls = true;
  expect((await $.command.run({ command: 'smart-router', args: 'off' })).text).toContain('Could not update Smart Router');
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('1');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(body);
});

test('an unreadable environment preserves orchestration and recorded usage', { options }, async ($, on) => {
  let invalidations = 0;
  on('ui.invalidate', { event: 'tool.describe' }, () => {
    invalidations += 1;
    return { value: undefined };
  });
  const { state, env, clock, row } = await savingsRow($, on);
  invalidations = 0;
  for (const denied of [true, false]) {
    state.denyEnvironmentRead = denied;
    for (const agentId of [undefined, 'child']) {
      const result = await $.turn.step({
        turnId: 'environment', index: 0, messageCount: 1,
        model: agentId ? 'claude-sonnet-4-6' : 'claude-opus-4-8', agentId,
      }).next();
      expect(result.value.answer).toBe('done');
    }
    expect(invalidations).toBe(0);
  }
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'main').usage.input_tokens).toBe(2);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'child').usage.input_tokens).toBe(102);
  expect(JSON.stringify(await row.drawn())).toContain('$0.18');
  env.ENABLE_SMART_ROUTER_ORCHESTRATOR = '0';
  await $.turn.step({ turnId: 'disabled', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
  expect(invalidations).toBe(1);
});

for (const reason of ['clear', 'resume'] as const) {
  test(reason + ' starts distinct session totals without old baselines or unknown usage', { options }, async ($, on) => {
    const { state, clock, row } = await savingsRow($, on, { incompleteUsage: true });
    state.selectedModel = 'claude-sonnet-4-6';
    await $.turn.step({ turnId: 'old', index: 0, messageCount: 1, model: state.selectedModel }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.usage.user_switched).toBe(true);
    const writes = state.writes;
    await $.session.end({ reason, sessionId: state.sessionId });
    await $.turn.complete({ answer: 'late completion', isAborted: false });
    await clock.advance(1);
    expect(state.writes).toBe(writes);
    state.sessionId = 'new-session';
    state.selectedModel = 'claude-haiku-4-5';
    await $.prompt.submit({ text: 'Next session task.' });
    await clock.advance(1);
    expect(state.usage).toEqual({
      version: 1, session_id: 'new-session', start_model: state.selectedModel,
      main_model: null, first_main_model: null, first_prompt_seen: true,
      user_switched: false, incomplete_usage: false, entries: [],
    });
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
    await $.turn.step({ turnId: 'new', index: 0, messageCount: 1, model: state.selectedModel }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.usage.entries.length).toBe(1);
    expect(state.usage.entries[0].baseline).toBe(state.selectedModel);
    expect(state.usage.entries[0].usage.input_tokens).toBe(1);
    expect(state.usage.incomplete_usage).toBe(false);
  });
}

test('process exit saves accepted usage after OFF without waiting for an optional price', { options }, async ($, on) => {
  const { state, clock, files } = await savingsRow($, on);
  let release!: () => void;
  state.priceBarrier = new Promise<void>(resolve => { release = resolve; });
  await $.turn.step({ turnId: 'first', index: 0, messageCount: 1, model: state.selectedModel }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'main').usage.input_tokens).toBe(1);
  await $.turn.step({ turnId: 'last', index: 0, messageCount: 1, model: state.selectedModel }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  state.controls.ENABLE_SMART_ROUTING_SAVINGS = '0';
  let ended = false;
  const ending = $.session.end({ reason: 'prompt_input_exit', sessionId: state.sessionId })
    .then(() => { ended = true; });
  try {
    await clock.advance(1);
    expect(ended).toBe(true);
  } finally {
    state.priceBarrier = null;
    release();
    await ending;
  }
  const saved = JSON.parse(files.get(`${options.savings_usage_dir}/session.json`)!);
  expect(saved.entries.find((entry: any) => entry.agent === 'main').usage.input_tokens).toBe(2);
  expect(saved.entries.find((entry: any) => entry.agent === 'child').usage.input_tokens).toBe(100);
  expect(saved.start_model).toBe('claude-opus-4-8');
  expect(saved.first_main_model).toBe('claude-opus-4-8');
});

test('reloading the same identified session preserves totals and accounting uncertainty', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on, { incompleteUsage: true });
  const saved = JSON.stringify(state.usage);
  await $.session.end({ reason: 'other' });
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/session' });
  await clock.advance(1);
  expect(JSON.stringify(state.usage)).toBe(saved);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
});

test('unidentified legacy session totals remain retained but cannot claim an estimate', { options }, async ($, on) => {
  const { state, row } = await savingsRow($, on, { legacySession: true });
  expect(state.usage.incomplete_usage).toBe(true);
  expect(state.usage.entries[0].usage.input_tokens).toBe(100);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
});

for (const metered of [true, false]) {
  test('an old ' + (metered ? 'metered' : 'unmetered') + ' request cannot alter a new session', { options }, async ($, on) => {
    const { state, clock } = await savingsRow($, on);
    let release!: () => void;
    const started = new Promise<void>(resolve => { state.stepStarted = resolve; });
    state.stepBarrier = new Promise<void>(resolve => { release = resolve; });
    const old = $.turn.step({ turnId: 'old', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
    await started;
    await $.session.end({ reason: 'clear' });
    state.sessionId = 'new-session';
    await $.prompt.submit({ text: 'Next session task.' });
    await clock.advance(1);
    state.responseUsage = metered ? { input_tokens: 99, output_tokens: 99 } : undefined;
    release();
    await old;
    state.stepBarrier = null;
    state.responseUsage = { input_tokens: 1, output_tokens: 1 };
    await $.turn.step({ turnId: 'new', index: 0, messageCount: 1, model: 'claude-opus-4-8' }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.usage.session_id).toBe('new-session');
    expect(state.usage.incomplete_usage).toBe(false);
    expect(state.usage.entries.length).toBe(1);
    expect(state.usage.entries[0].usage.input_tokens).toBe(1);
  });
}

for (const transition of ['clear', 'same-session resume', 'cross-session resume']) {
  for (const pending of ['price', 'save'] as const) {
    test(transition + ' retains both old updates while a ' + pending + ' is pending', { options }, async ($, on) => {
      const { state, clock, row, files } = await savingsRow($, on);
      let release!: () => void;
      const barrier = new Promise<void>(resolve => { release = resolve; });
      if (pending === 'price') state.priceBarrier = barrier;
      else state.writeBarrier = barrier;
      await $.turn.step({ turnId: 'old-first', index: 0, messageCount: 1, model: state.selectedModel }).next();
      await $.turn.complete({ answer: 'done', isAborted: false });
      await clock.advance(1);
      await $.turn.step({ turnId: 'old-second', index: 0, messageCount: 1, model: state.selectedModel }).next();
      await $.turn.complete({ answer: 'done', isAborted: false });
      await $.session.end({ reason: transition === 'clear' ? 'clear' : 'resume', sessionId: state.sessionId });
      const same = transition === 'same-session resume';
      if (!same) state.sessionId = 'new-session';
      let submitted = false;
      const start = $.prompt.submit({ text: 'Next session task.' }).then(() => { submitted = true; });
      try {
        await clock.advance(1);
        expect(submitted).toBe(true);
        const request = await $.turn.step({ turnId: 'new', index: 0, messageCount: 1, model: state.selectedModel }).next();
        expect(request.done).toBe(true);
        expect(request.value.answer).toBe('done');
        await $.turn.complete({ answer: 'done', isAborted: false });
        expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
      } finally {
        state.priceBarrier = null;
        state.writeBarrier = null;
        release();
        await start;
      }
      await clock.advance(1);
      const old = JSON.parse(files.get(`${options.savings_usage_dir}/session.json`)!);
      expect(old.entries.find((entry: any) => entry.agent === 'main').usage.input_tokens).toBe(same ? 3 : 2);
      expect(old.entries.find((entry: any) => entry.agent === 'child').usage.input_tokens).toBe(100);
      expect(old.start_model).toBe('claude-opus-4-8');
      if (!same) {
        const current = JSON.parse(files.get(`${options.savings_usage_dir}/new-session.json`)!);
        expect(current.entries.length).toBe(1);
        expect(current.entries[0].usage.input_tokens).toBe(1);
        expect(current.incomplete_usage).toBe(false);
      }
    });
  }
}

test('an ordinary prompt resumes the same session without accepting its late request', { options }, async ($, on) => {
  const { state, clock } = await savingsRow($, on);
  let release!: () => void;
  const started = new Promise<void>(resolve => { state.stepStarted = resolve; });
  state.stepBarrier = new Promise<void>(resolve => { release = resolve; });
  const old = $.turn.step({ turnId: 'old', index: 0, messageCount: 1, model: state.selectedModel }).next();
  await started;
  const saved = JSON.stringify(state.usage);
  await $.session.end({ reason: 'resume', sessionId: state.sessionId });
  await $.turn.complete({ answer: 'late completion', isAborted: false });
  await clock.advance(1);
  expect(JSON.stringify(state.usage)).toBe(saved);
  await $.prompt.submit({ text: 'Continue this session.' });
  state.responseUsage = { input_tokens: 99, output_tokens: 99 };
  release();
  await old;
  state.stepBarrier = null;
  state.responseUsage = { input_tokens: 1, output_tokens: 1 };
  await $.turn.step({ turnId: 'new', index: 0, messageCount: 1, model: state.selectedModel }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.session_id).toBe('session');
  expect(state.usage.incomplete_usage).toBe(false);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'child').usage.input_tokens).toBe(100);
  expect(state.usage.entries.find((entry: any) => entry.agent === 'main')?.usage.input_tokens).toBe(1);
});

test('renders without an estimate do not read savings controls', { options }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on, { incompleteUsage: true });
  const reads = state.savingsFlagReads;
  const another = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} });
  await clock.advance(1);
  expect(JSON.stringify(await another.drawn())).not.toContain('$0.18');
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  expect(state.savingsFlagReads).toBe(reads);
});


for (const usageFile of ['unreadable', 'malformed', 'unsupported', 'invalid_entry'] as const) {
  test('an ' + usageFile + ' usage snapshot is retained while ordinary requests complete', { options }, async ($, on) => {
    const { state, clock, row } = await savingsRow($, on, { usageFile });
    await $.prompt.submit({ text: 'Continue this session.' });
    const result = await $.turn.step({ turnId: 'ordinary', index: 0, messageCount: 1, model: state.selectedModel }).next();
    expect(result.value.answer).toBe('done');
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.writes).toBe(0);
    expect(state.usage).toBe(null);
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  });
}

test('a missing usage file starts fresh accounting', { options }, async ($, on) => {
  const { state, clock } = await savingsRow($, on, { usageFile: 'missing' });
  await $.turn.step({ turnId: 'first', index: 0, messageCount: 1, model: state.selectedModel }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  expect(state.usage.session_id).toBe('session');
  expect(state.usage.incomplete_usage).toBe(false);
  expect(state.usage.entries.length).toBe(1);
  expect(state.usage.entries[0].usage.input_tokens).toBe(1);
});

for (const model of ['claude-sonnet-4-6', 'claude-haiku-4-5']) {
  test('a restored routed child retains its accounting when resumed on ' + model, { options }, async ($, on) => {
    const { state, clock } = await savingsRow($, on);
    await $.session.end({ reason: 'resume', sessionId: state.sessionId });
    await $.prompt.submit({ text: 'Continue this session.' });
    await $.turn.step({ turnId: 'resumed-child', index: 0, messageCount: 1, model, agentId: 'child' }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.requestModel).toBe(model);
    const entries = state.usage.entries.filter((entry: any) => entry.agent === 'child');
    expect(entries.every((entry: any) => entry.routed && entry.baseline === state.selectedModel)).toBe(true);
    expect(entries.reduce((sum: number, entry: any) => sum + entry.usage.input_tokens, 0)).toBe(101);
    expect(entries.find((entry: any) => entry.served === model).usage.input_tokens).toBe(model === 'claude-sonnet-4-6' ? 101 : 1);
  });
}

test('a cost increase replaces the routing band and cached renders do not reread controls', { options: { ...options, orchestrator_version: '1.2.0' } }, async ($, on) => {
  const { state, clock, row } = await savingsRow($, on);
  state.saved = false;
  await $.turn.step({ turnId: 'costlier', index: 0, messageCount: 1, model: state.selectedModel }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  // A successful native toggle would otherwise show the normal status band again.
  await $.command.run({ command: 'smart-router', args: 'on' });
  await clock.advance(1);
  const reads = state.controlReads;
  const flags = state.savingsFlagReads;
  const another = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} });
  await clock.advance(1);
  const rendered = JSON.stringify(await another.drawn());
  expect(rendered).toContain('Smart routing cost');
  expect(rendered).toContain('more (60%)');
  expect(rendered).toContain('— Unity Gateway Smart Router v1.2.0');
  expect(rendered).not.toContain('unity gateway smart router');
  expect(rendered).not.toContain('— routing subagents');
  expect(state.controlReads).toBe(reads);
  expect(state.savingsFlagReads).toBe(flags);
  state.showEstimate = false;
  await $.turn.step({ turnId: 'unpriced', index: 0, messageCount: 1, model: state.selectedModel }).next();
  await $.turn.complete({ answer: 'done', isAborted: false });
  await clock.advance(1);
  const band = JSON.stringify(await row.drawn());
  expect(band).toContain('unity gateway smart router');
  expect(band).toContain('— routing subagents');
  await $.command.run({ command: 'smart-router', args: 'off' });
  await clock.advance(1);
  expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
});


for (const [label, usage] of [
  ['fraction', { input_tokens: 0.5, output_tokens: 10 }],
  ['negative', { input_tokens: 7, output_tokens: -1 }],
  ['infinite', { input_tokens: 7, cache_creation_input_tokens: Infinity }],
  ['NaN', { input_tokens: 7, cache_read_input_tokens: NaN }],
  ['boolean', { input_tokens: true, output_tokens: 10 }],
  ['split fraction', { input_tokens: 7, cache_creation: { ephemeral_5m_input_tokens: 0.5 } }],
  ['split boolean', { input_tokens: 7, cache_creation: { ephemeral_1h_input_tokens: false } }],
] as const) {
  test('a saved ' + label + ' token count hides the estimate without replacing the snapshot', { options }, async ($, on) => {
    const { state, clock, row, files } = await savingsRow($, on, { invalidSavedUsage: usage });
    const result = await $.turn.step({ turnId: 'ordinary', index: 0, messageCount: 1, model: state.selectedModel }).next();
    expect(result.value.answer).toBe('done');
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.writes).toBe(0);
    expect(files.size).toBe(0);
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  });

  test('a returned ' + label + ' token count preserves earlier usage and marks it incomplete', { options }, async ($, on) => {
    const { state, clock, row } = await savingsRow($, on);
    const entries = JSON.stringify(state.usage.entries);
    state.responseUsage = usage;
    await $.turn.step({ turnId: 'malformed', index: 0, messageCount: 1, model: state.selectedModel }).next();
    await $.turn.complete({ answer: 'done', isAborted: false });
    await clock.advance(1);
    expect(state.usage.incomplete_usage).toBe(true);
    expect(JSON.stringify(state.usage.entries)).toBe(entries);
    expect(JSON.stringify(await row.drawn())).not.toContain('$0.18');
  });
}
