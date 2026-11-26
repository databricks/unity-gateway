import type { AgentInfo, On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';

const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;
const path = '/launch/subagents.json';

function engine(on: On, agents: AgentInfo[]) {
  const clock = mock.clock(on);
  const writes: { sessionId: string; activeTasks: number | null }[] = [];
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
  expect(writes.at(-1)).toEqual({ sessionId: 'session-1', activeTasks: 2 });
  await $.turn.complete({ answer: 'Waiting for delegated work.' });
  await clock.advance(1000);
  expect(writes.at(-1)).toEqual({ sessionId: 'session-1', activeTasks: 2 });
  agents[0] = { ...agents[0], status: 'completed' };
  await clock.advance(1000);
  expect(writes.at(-1)).toEqual({ sessionId: 'session-1', activeTasks: 1 });
  agents[1] = { ...agents[1], status: 'completed' };
  await clock.advance(1000);
  expect(writes.at(-1)).toEqual({ sessionId: 'session-1', activeTasks: 0 });
});

test('held and pending agents still have outstanding work, without inventing an input request', async ($, on) => {
  const statuses: AgentInfo['status'][] = ['pending', 'running', 'waiting', 'idle', 'completed', 'failed', 'killed'];
  const agents = statuses.map((status, i) => ({ id: String(i), description: '', type: 'worker', status }));
  const { writes } = engine(on, agents);
  await $.session.start(start);
  expect(writes).toEqual([{ sessionId: 'session-1', activeTasks: 3 }]);
});

test('failed registry reads and failed writes are retryable and never fabricate completion', async ($, on) => {
  const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'running' }];
  const { state, writes, clock } = engine(on, agents);
  await $.session.start(start);
  agents.length = 0;
  state.readError = true;
  await clock.advance(1000);
  expect(writes).toEqual([{ sessionId: 'session-1', activeTasks: 1 }]);
  state.readError = false;
  state.writeError = true;
  await clock.advance(1000);
  expect(writes).toHaveLength(1);
  state.writeError = false;
  await clock.advance(1000);
  expect(writes.at(-1)).toEqual({ sessionId: 'session-1', activeTasks: 0 });
});

for (const reason of ['clear', 'resume'] as const) {
  test(`${reason} clears old work and observes the replacement without another session.start`, async ($, on) => {
    const agents: AgentInfo[] = [{ id: 'a', description: '', type: 'worker', status: 'running' }];
    const { state, writes, clock } = engine(on, agents);
    await $.session.start(start);
    await $.session.end({ reason, sessionId: 'session-1' });
    expect(writes.at(-1)).toEqual({ sessionId: 'session-1', activeTasks: null });
    await clock.advance(1000);
    expect(writes).toHaveLength(2);
    state.sessionId = 'session-2';
    await clock.advance(1000);
    expect(writes.at(-1)).toEqual({ sessionId: 'session-2', activeTasks: 1 });
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
  expect(writes.at(-1)).toEqual({ sessionId: 'session-1', activeTasks: null });
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
