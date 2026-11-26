import type { AgentInfo, On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';

const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;
const path = '/launch/subagents.json';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}

function questionTools(on: On) {
  const calls = new Map<string, { started: ReturnType<typeof deferred>; gate: ReturnType<typeof deferred>; cancelled: boolean }>();
  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e) => {
    const call = calls.get(e.tool_use_id)!;
    call.started.resolve();
    await call.gate.promise;
    return call.cancelled ? { deny: 'question cancelled' } : { result: { answers: {} } };
  });
  return (id: string, cancelled = false) => {
    const call = { started: deferred(), gate: deferred(), cancelled };
    calls.set(id, call);
    return { started: call.started.promise, finish: call.gate.resolve };
  };
}

function engine(on: On, agents: AgentInfo[]) {
  const clock = mock.clock(on);
  const writes: { sessionId: string; agents: { id: string; status: string; title: string; kind?: string }[] | null }[] = [];
  const env: Record<string, string | undefined> = { UG_CLAUDE_PROGRAM_STATUS_FILE: path };
  const state: {
    sessionId: string; readError: boolean; writeError: boolean; reads: number;
    beforeRead?: () => Promise<void>;
  } = { sessionId: 'session-1', readError: false, writeError: false, reads: 0 };
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('session.id', () => ({ value: state.sessionId }));
  on('fs.exists', () => ({ value: false }));
  on('fs.write', ($, e) => {
    if (state.writeError) throw new Error('write failed');
    if (e.path === path) writes.push(JSON.parse(e.text));
    return { value: undefined };
  });
  on('agent.list', async () => {
    state.reads += 1;
    await state.beforeRead?.();
    if (state.readError) throw new Error('read failed');
    return { value: [...agents] };
  });
  on('ui.invalidate', () => ({ value: undefined }));
  on('session.start', () => ({ cwd: '/work' }));
  on('turn.complete', () => ({ text: '' }));
  on('session.end', ($, e) => ({ sessionId: e.sessionId }));
  return { clock, writes, env, state };
}

test('delegated work remains active after the main turn ends, without drawing a footer', async ($, on) => {
  const agents: AgentInfo[] = [
    { id: 'a', description: 'Research', type: 'researcher', status: 'running' },
    { id: 'b', description: 'Explore', type: 'explorer', status: 'running' },
  ];
  const { clock, writes } = engine(on, agents);
  await $.session.start(start);
  expect(writes.at(-1)).toEqual({ sessionId: 'session-1', agents: [
    { id: 'a', status: 'running', title: 'Research' },
    { id: 'b', status: 'running', title: 'Explore' },
  ] });
  await $.turn.complete({ answer: 'Waiting for delegated work.' });
  await clock.advance(1000);
  expect(writes.at(-1)?.agents?.map(agent => [agent.id, agent.status])).toEqual([['a', 'running'], ['b', 'running']]);
  agents[0] = { ...agents[0], status: 'completed' };
  await clock.advance(1000);
  expect(writes.at(-1)?.agents?.map(agent => [agent.id, agent.status])).toEqual([['a', 'completed'], ['b', 'running']]);
  agents[1] = { ...agents[1], status: 'completed' };
  await clock.advance(1000);
  expect(writes.at(-1)?.agents?.map(agent => [agent.id, agent.status])).toEqual([['a', 'completed'], ['b', 'completed']]);
});

test('a displayed subagent question reports its reason until that exact call resolves', async ($, on) => {
  const agents: AgentInfo[] = [{ id: 'a', description: 'Research', type: 'worker', status: 'running' }];
  const { clock, writes } = engine(on, agents);
  let release!: () => void;
  let entered!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const gate = new Promise<void>(resolve => { release = resolve; });
  on('tool.call', { tool: 'AskUserQuestion' }, async () => {
    entered();
    await gate;
    return { result: { answers: {} } };
  });
  await $.session.start(start);
  const call = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: 'question-1', agentId: 'a', questions: [] });
  await started;
  await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId: 'question-1', props: { tool: 'AskUserQuestion', questions: [] } });
  await clock.advance(1000);
  const asking = writes.at(-1)?.agents;
  release();
  const result = await call;
  await clock.advance(1000);
  expect(asking).toEqual([{ id: 'a', status: 'waiting', title: 'Research', kind: 'question' }]);
  expect(writes.at(-1)?.agents).toEqual([{ id: 'a', status: 'running', title: 'Research' }]);
  expect(result.result).toEqual({ answers: {} });
});

test('automatic answers and late dialog redraws never invent a request for input', async ($, on) => {
  const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'waiting' }];
  const { clock, writes } = engine(on, agents);
  const prepare = questionTools(on);
  await $.session.start(start);
  const question = prepare('automatic');
  const call = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: 'automatic', agentId: 'a', questions: [] });
  await question.started;
  await clock.advance(1000);
  const during = writes.at(-1);
  question.finish();
  await call;
  await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId: 'automatic', props: { tool: 'AskUserQuestion', questions: [] } });
  await clock.advance(1000);
  expect(during?.agents).toEqual([{ id: 'a', status: 'waiting', title: 'worker' }]);
  expect(writes).toHaveLength(1);
});

test('a main-agent question and an unrelated dialog do not change a subagent', async ($, on) => {
  const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'running' }];
  const { clock, writes } = engine(on, agents);
  const prepare = questionTools(on);
  await $.session.start(start);
  const question = prepare('main');
  const call = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: 'main', questions: [] });
  await question.started;
  for (const requestId of ['main', 'unknown']) {
    await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId, props: { tool: 'AskUserQuestion', questions: [] } });
  }
  await clock.advance(1000);
  question.finish();
  await call;
  expect(writes).toHaveLength(1);
  expect(writes.at(-1)?.agents).toEqual([{ id: 'a', status: 'running', title: 'worker' }]);
});

test('answering one question preserves the other questions for that agent and its sibling', async ($, on) => {
  const agents: AgentInfo[] = ['a', 'b'].map(id => ({ id, description: '', type: 'worker', status: 'running' }));
  const { clock, writes } = engine(on, agents);
  const prepare = questionTools(on);
  await $.session.start(start);
  const requests = [];
  for (const [id, agentId] of [['a1', 'a'], ['a2', 'a'], ['b1', 'b']]) {
    const question = prepare(id);
    const call = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: id, agentId, questions: [] });
    await question.started;
    await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId: id, props: { tool: 'AskUserQuestion', questions: [] } });
    requests.push({ question, call });
  }
  await clock.advance(1000);
  const bothAsking = writes.at(-1)?.agents;
  requests[0].question.finish();
  await requests[0].call;
  await clock.advance(1000);
  const afterFirst = writes.at(-1)?.agents;
  requests[2].question.finish();
  await requests[2].call;
  await clock.advance(1000);
  const afterSibling = writes.at(-1)?.agents;
  requests[1].question.finish();
  await requests[1].call;
  await clock.advance(1000);
  expect(bothAsking?.map(agent => [agent.id, agent.kind])).toEqual([['a', 'question'], ['b', 'question']]);
  expect(afterFirst).toEqual(bothAsking);
  expect(afterSibling?.map(agent => [agent.id, agent.kind])).toEqual([['a', 'question'], ['b', undefined]]);
  expect(writes.at(-1)?.agents?.map(agent => agent.kind)).toEqual([undefined, undefined]);
});

test('cancelling a displayed question clears its reason and preserves the cancellation', async ($, on) => {
  const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'waiting' }];
  const { clock, writes } = engine(on, agents);
  const prepare = questionTools(on);
  await $.session.start(start);
  const question = prepare('cancelled', true);
  const call = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: 'cancelled', agentId: 'a', questions: [] });
  await question.started;
  await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId: 'cancelled', props: { tool: 'AskUserQuestion', questions: [] } });
  await clock.advance(1000);
  const asking = writes.at(-1)?.agents?.[0].kind;
  question.finish();
  const result = await call;
  await clock.advance(1000);
  expect(asking).toBe('question');
  expect(result.deny).toBe('question cancelled');
  expect(writes.at(-1)?.agents).toEqual([{ id: 'a', status: 'waiting', title: 'worker' }]);
});

for (const reason of ['clear', 'resume', 'replacement'] as const) {
  test(`${reason} invalidates old dialogs and old completions while a new question is open`, async ($, on) => {
    const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'running' }];
    const { clock, writes, state } = engine(on, agents);
    const prepare = questionTools(on);
    await $.session.start(start);
    const old = prepare('old');
    const oldCall = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: 'old', agentId: 'a', questions: [] });
    await old.started;
    await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId: 'old', props: { tool: 'AskUserQuestion', questions: [] } });
    await clock.advance(1000);
    if (reason !== 'replacement') await $.session.end({ reason, sessionId: 'session-1' });
    state.sessionId = 'session-2';
    await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId: 'old', props: { tool: 'AskUserQuestion', questions: [] } });
    await clock.advance(1000);
    const replacement = writes.at(-1);
    const current = prepare('new');
    const currentCall = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: 'new', agentId: 'a', questions: [] });
    await current.started;
    await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId: 'new', props: { tool: 'AskUserQuestion', questions: [] } });
    old.finish();
    await oldCall;
    await clock.advance(1000);
    const currentStatus = writes.at(-1);
    current.finish();
    await currentCall;
    await clock.advance(1000);
    expect(replacement).toEqual({ sessionId: 'session-2', agents: [{ id: 'a', status: 'running', title: 'worker' }] });
    expect(currentStatus).toEqual({ sessionId: 'session-2', agents: [{ id: 'a', status: 'waiting', title: 'worker', kind: 'question' }] });
    expect(writes.at(-1)?.agents?.[0].kind).toBeUndefined();
  });
}

test('question reasons recover after read and write failures, including an answer during a failed write', async ($, on) => {
  const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'running' }];
  const { clock, writes, state } = engine(on, agents);
  const prepare = questionTools(on);
  await $.session.start(start);
  const question = prepare('recoverable');
  const call = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: 'recoverable', agentId: 'a', questions: [] });
  await question.started;
  await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId: 'recoverable', props: { tool: 'AskUserQuestion', questions: [] } });
  state.readError = true;
  await clock.advance(1000);
  state.readError = false;
  state.writeError = true;
  await clock.advance(1000);
  const failedPublications = writes.length;
  state.writeError = false;
  await clock.advance(1000);
  const asking = writes.at(-1)?.agents;
  state.writeError = true;
  question.finish();
  await call;
  await clock.advance(1000);
  const unresolvedPublication = writes.at(-1)?.agents;
  state.writeError = false;
  await clock.advance(1000);
  expect(failedPublications).toBe(1);
  expect(asking).toEqual([{ id: 'a', status: 'waiting', title: 'worker', kind: 'question' }]);
  expect(unresolvedPublication).toEqual(asking);
  expect(writes.at(-1)?.agents).toEqual([{ id: 'a', status: 'running', title: 'worker' }]);
});

for (const status of ['completed', 'failed', 'killed'] as const) {
  test(`an unresolved question cannot override an agent that is ${status}`, async ($, on) => {
    const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'running' }];
    const { clock, writes } = engine(on, agents);
    const prepare = questionTools(on);
    await $.session.start(start);
    const question = prepare('ended-agent');
    const call = $.tool.call({ tool: 'AskUserQuestion', tool_use_id: 'ended-agent', agentId: 'a', questions: [] });
    await question.started;
    await $.ui.render({ surface: 'terminal', component: 'AskUserQuestion', requestId: 'ended-agent', props: { tool: 'AskUserQuestion', questions: [] } });
    agents[0] = { ...agents[0], status };
    await clock.advance(1000);
    question.finish();
    await call;
    expect(writes.at(-1)?.agents).toEqual([{ id: 'a', status, title: 'worker' }]);
  });
}

test('every agent lifecycle state is preserved independently for the terminal', async ($, on) => {
  const statuses: AgentInfo['status'][] = ['pending', 'running', 'waiting', 'idle', 'completed', 'failed', 'killed'];
  const agents = statuses.map((status, i) => ({ id: String(i), description: '', type: 'worker', status }));
  const { writes } = engine(on, agents);
  await $.session.start(start);
  expect(writes.at(-1)?.agents?.map(agent => agent.status)).toEqual(statuses);
});

test('failed registry reads and failed writes are retryable and never fabricate completion', async ($, on) => {
  const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'running' }];
  const { state, writes, clock } = engine(on, agents);
  await $.session.start(start);
  agents.length = 0;
  state.readError = true;
  await clock.advance(1000);
  expect(writes).toEqual([{ sessionId: 'session-1', agents: [{ id: 'a', status: 'running', title: 'worker' }] }]);
  state.readError = false;
  state.writeError = true;
  await clock.advance(1000);
  expect(writes).toHaveLength(1);
  state.writeError = false;
  await clock.advance(1000);
  expect(writes.at(-1)).toEqual({ sessionId: 'session-1', agents: [] });
});

for (const reason of ['clear', 'resume'] as const) {
  test(`${reason} clears old work and observes the replacement without another session.start`, async ($, on) => {
    const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'running' }];
    const { state, writes, clock } = engine(on, agents);
    await $.session.start(start);
    await $.session.end({ reason, sessionId: 'session-1' });
    expect(writes.at(-1)).toEqual({ sessionId: 'session-1', agents: null });
    await clock.advance(1000);
    expect(writes).toHaveLength(2);
    state.sessionId = 'session-2';
    await clock.advance(1000);
    expect(writes.at(-1)).toEqual({ sessionId: 'session-2', agents: [{ id: 'a', status: 'running', title: 'worker' }] });
  });
}

test('a slow scan cannot publish old working status after the conversation is cleared', async ($, on) => {
  const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'running' }];
  const { state, writes, clock } = engine(on, agents);
  await $.session.start(start);
  let release!: () => void;
  let entered!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const gate = new Promise<void>(resolve => { release = resolve; });
  state.beforeRead = async () => { entered(); await gate; };
  const scan = clock.advance(1000);
  await started;
  const end = $.session.end({ reason: 'clear', sessionId: 'session-1' });
  release();
  await Promise.all([scan, end]);
  expect(writes.at(-1)).toEqual({ sessionId: 'session-1', agents: null });
  expect(writes).toHaveLength(2);
});

for (const mode of ['missing-path', 'opt-out', 'headless'] as const) {
  test(`${mode} never reads the registry or writes a terminal-status snapshot`, async ($, on) => {
    const { env, state, writes, clock } = engine(on, []);
    if (mode === 'missing-path') env.UG_CLAUDE_PROGRAM_STATUS_FILE = undefined;
    if (mode === 'opt-out') env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE = '1';
    await $.session.start({ ...start, isInteractive: mode !== 'headless' });
    await clock.advance(3000);
    expect(state.reads).toBe(0);
    expect(writes).toHaveLength(0);
  });
}


test('names and failures survive while another agent continues working', async ($, on) => {
  const agents: AgentInfo[] = [
    { id: 'b', description: 'Explore parser', type: 'explorer', status: 'running' },
    { id: 'a', description: 'Read metrics', type: 'researcher', status: 'running' },
  ];
  const { clock, writes } = engine(on, agents);
  await $.session.start(start);
  agents.reverse();
  await clock.advance(1000);
  expect(writes).toHaveLength(1);
  agents[0] = { ...agents[0], status: 'failed' };
  await clock.advance(1000);
  expect(writes.at(-1)?.agents).toEqual([
    { id: 'a', status: 'failed', title: 'Read metrics' },
    { id: 'b', status: 'running', title: 'Explore parser' },
  ]);
});

test('an agent with no description uses its name or role without changing its identity', async ($, on) => {
  const agents: AgentInfo[] = [{ id: 'a', description: '', name: 'Otter', type: 'researcher', status: 'running' }];
  const { clock, writes } = engine(on, agents);
  await $.session.start(start);
  expect(writes.at(-1)?.agents).toEqual([{ id: 'a', status: 'running', title: 'Otter' }]);
  agents[0] = { ...agents[0], name: undefined };
  await clock.advance(1000);
  expect(writes.at(-1)?.agents).toEqual([{ id: 'a', status: 'running', title: 'researcher' }]);
});
