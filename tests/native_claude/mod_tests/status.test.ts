import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';

const DIRECT = 'Main agent is not spawning subagents and is working on the task directly.';
const SPAWNING = 'Main agent is spawning subagents.';
const TOOL = 'mcp__ug-smart-router__working_directly';
const OPTIONS = {
  ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1', unset: [], recipe: 'task_v3',
};

function setup(on: On, drop = '') {
  const env: Record<string, string | undefined> = {
    ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: '1', UCODE_SESSION_ENV_FILE: '/work/session.json',
  };
  let controls = '{}';
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => { env[e.name] = e.value; return { value: undefined }; });
  on('fs.read', () => ({ value: controls }));
  on('tool.register', () => ({ value: { tool: TOOL } }));
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e);
    return Text({ children: ['other plugin'] });
  });
  on('session.start', () => ({ cwd: '/work' }));
  on('turn.start', ($, e) => ({ turnId: e.turnId }));
  on('turn.complete', ($, e) => ({ text: e.answer }));
  on('prompt.submit', ($, e) => e.text === drop && drop ? { drop: 'blocked prompt' } : { text: e.text, context: e.context });
  on('session.end', ($, e) => ({ sessionId: e.sessionId }));
  // Downstream denial is deliberately preserved: requesting a spawn reports
  // the choice without claiming that a child actually started.
  on('tool.call', () => ({ deny: 'native permission refusal' }));
  return { env, override: (value: string) => { controls = value; } };
}

test('one indicator follows the main request choice, forwards native permissions and clears for the next request', { options: OPTIONS }, async ($, on) => {
  setup(on);
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' });
  const ui = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} as any });
  const text = async () => JSON.stringify(await ui.drawn());
  expect(await text()).toContain('unity gateway smart router');
  expect(await text()).not.toContain(DIRECT);
  expect(await text()).not.toContain(SPAWNING);

  const prompt = await $.prompt.submit({ text: 'first task', wait: false, origin: { kind: 'composer' } });
  expect(prompt.context?.join('\n')).toContain(TOOL);
  await $.turn.start({ text: 'first task', turnId: 'request-1' });
  expect((await $.tool.call({ tool: TOOL })).result).toEqual('Choice reported.');
  expect(await text()).toContain(DIRECT);
  expect(await text()).not.toContain(SPAWNING);

  const args = { tool: 'Agent', description: 'inspect', prompt: 'inspect files', subagent_type: 'Explore' };
  expect(await $.tool.call(args as any)).toEqual({ deny: 'native permission refusal' });
  expect(await text()).toContain(SPAWNING);
  expect(await text()).not.toContain(DIRECT);
  await $.tool.call({ tool: TOOL });
  expect(await text()).toContain(SPAWNING);

  await $.prompt.submit({ text: 'second task', wait: false, origin: { kind: 'composer' } });
  await $.turn.start({ text: 'second task', turnId: 'request-2' });
  expect(await text()).not.toContain(DIRECT);
  expect(await text()).not.toContain(SPAWNING);
  await $.tool.call({ ...args, tool: 'Task' } as any);
  expect(await text()).toContain(SPAWNING);
  await $.session.end({ reason: 'clear', sessionId: 'session-1', resume: { id: 'session-1' } });
  expect(await text()).not.toContain(SPAWNING);
  await ui.unmount();
});

test('child calls cannot report or change the main agent choice', { options: OPTIONS }, async ($, on) => {
  setup(on);
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' });
  await $.turn.start({ text: 'root task', turnId: 'root-request' });
  const ui = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} as any });
  await $.tool.call({ tool: 'Agent', agentId: 'child', description: 'nested', prompt: 'nested', subagent_type: 'Explore' } as any);
  expect(JSON.stringify(await ui.drawn())).not.toContain(SPAWNING);
  expect(await $.tool.call({ tool: TOOL, agentId: 'child' } as any)).toEqual({ deny: 'Only the main agent can report its choice.' });
  expect(JSON.stringify(await ui.drawn())).not.toContain(DIRECT);
  await $.tool.call({ tool: TOOL });
  await $.tool.call({ tool: 'Task', agentId: 'child', description: 'nested', prompt: 'nested', subagent_type: 'Explore' } as any);
  expect(JSON.stringify(await ui.drawn())).toContain(DIRECT);
  await ui.unmount();
});

test('completion rejects late root reports while child notifications preserve the request choice', { options: OPTIONS }, async ($, on) => {
  setup(on);
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' });
  await $.prompt.submit({ text: 'task', wait: false, origin: { kind: 'composer' } });
  await $.turn.start({ text: 'task', turnId: 'request' });
  const complete = { answer: 'done', durationMs: 1, isAborted: false, reason: 'answer', turnId: 'request' } as const;
  await $.turn.complete({ ...complete, agentId: 'child' });
  expect((await $.tool.call({ tool: TOOL })).result).toEqual('Choice reported.');
  await $.turn.complete(complete);
  expect((await $.tool.call({ tool: TOOL })).result).toEqual('Choice not reported: no active main-agent request.');
  await $.tool.call({ tool: 'Agent', description: 'late', prompt: 'late', subagent_type: 'Explore' } as any);
  const ui = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} as any });
  expect(JSON.stringify(await ui.drawn())).toContain(DIRECT);

  const notification = await $.prompt.submit({ text: 'child finished', wait: false, origin: { kind: 'task-notification' } });
  expect(notification.context).toBeUndefined();
  await $.turn.start({ text: 'child finished', turnId: 'continuation' });
  expect(JSON.stringify(await ui.drawn())).toContain(DIRECT);
  await $.prompt.submit({ text: '', wait: false, origin: { kind: 'composer' } });
  await $.turn.start({ text: '', turnId: 'image-request' });
  expect(JSON.stringify(await ui.drawn())).not.toContain(DIRECT);
  await ui.unmount();
});

test('the indicator stays after the band hides for the turn', { options: OPTIONS }, async ($, on) => {
  setup(on);
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' });
  const ui = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} as any });
  await $.prompt.submit({ text: 'task', wait: false, origin: { kind: 'composer' } });
  await $.turn.start({ text: 'task', turnId: 'request' });
  await $.tool.call({ tool: 'Agent', description: 'inspect', prompt: 'inspect', subagent_type: 'Explore' } as any);
  await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, reason: 'answer', turnId: 'request' });
  const drawn = JSON.stringify(await ui.drawn());
  expect(drawn).not.toContain('unity gateway smart router');
  expect(drawn).toContain(SPAWNING);
  await ui.unmount();
});

test('a peer completion message from a child keeps the spawning choice', { options: OPTIONS }, async ($, on) => {
  setup(on);
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' });
  await $.prompt.submit({ text: 'task', wait: false, origin: { kind: 'composer' } });
  await $.turn.start({ text: 'task', turnId: 'request' });
  await $.tool.call({ tool: 'Agent', description: 'inspect', prompt: 'inspect', subagent_type: 'Explore' } as any);
  await $.turn.complete({ answer: 'started', durationMs: 1, isAborted: false, reason: 'answer', turnId: 'request' });
  const message = await $.prompt.submit({ text: 'child finished', wait: false, origin: { kind: 'peer' } as any });
  expect(message.context).toBeUndefined();
  await $.turn.start({ text: 'child finished', turnId: 'continuation' });
  const ui = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} as any });
  expect(JSON.stringify(await ui.drawn())).toContain(SPAWNING);
  await ui.unmount();
});

test('a dropped prompt does not clear the choice on a later child notification', { options: OPTIONS }, async ($, on) => {
  setup(on, 'blocked');
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' });
  await $.prompt.submit({ text: 'task', wait: false, origin: { kind: 'composer' } });
  await $.turn.start({ text: 'task', turnId: 'request' });
  await $.tool.call({ tool: TOOL });
  expect(await $.prompt.submit({ text: 'blocked', wait: false, origin: { kind: 'composer' } })).toEqual({ drop: 'blocked prompt' });
  await $.prompt.submit({ text: 'child finished', wait: false, origin: { kind: 'task-notification' } });
  await $.turn.start({ text: 'child finished', turnId: 'continuation' });
  const ui = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} as any });
  expect(JSON.stringify(await ui.drawn())).toContain(DIRECT);
  await ui.unmount();
});

test('session overrides and orchestration opt-out hide the indicator and reporting instructions', { options: OPTIONS }, async ($, on) => {
  const { env, override } = setup(on);
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' });
  await $.turn.start({ text: 'task', turnId: 'request' });
  await $.tool.call({ tool: TOOL });
  const ui = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} as any });
  expect(JSON.stringify(await ui.drawn())).toContain(DIRECT);
  override('{"ENABLE_SMART_ROUTING_SUBAGENT_ONLY":"0","ENABLE_SMART_ROUTER_ORCHESTRATOR":"0"}');
  await ui.redraw();
  expect(JSON.stringify(await ui.drawn())).not.toContain(DIRECT);
  expect((await $.prompt.submit({ text: 'off', wait: false, origin: { kind: 'composer' } })).context).toBeUndefined();
  override('{}');
  env.ISAAC_LAUNCH_MODE = ' Omni ';
  await ui.redraw();
  expect(JSON.stringify(await ui.drawn())).not.toContain(DIRECT);
  env.ISAAC_LAUNCH_MODE = undefined;
  override('not json');
  await ui.redraw();
  expect(JSON.stringify(await ui.drawn())).not.toContain(DIRECT);
  expect(JSON.stringify(await ui.drawn())).toContain('unity gateway smart router');
  await ui.unmount();
});

test('full routing also reports the choice', { options: { ...OPTIONS, ENABLE_SMART_ROUTING_V2: '1', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0' } }, async ($, on) => {
  setup(on);
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' });
  await $.turn.start({ text: 'task', turnId: 'request' });
  await $.tool.call({ tool: TOOL });
  const ui = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} as any });
  expect(JSON.stringify(await ui.drawn())).toContain(DIRECT);
  expect(JSON.stringify(await ui.drawn())).toContain('routing first prompt + subagents');
  await ui.unmount();
});

test('routing without orchestration keeps the ordinary banner and prompt', { options: { ...OPTIONS, ENABLE_SMART_ROUTER_ORCHESTRATOR: '0' } }, async ($, on) => {
  setup(on);
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' });
  await $.turn.start({ text: 'task', turnId: 'request' });
  await $.tool.call({ tool: 'Agent', description: 'inspect', prompt: 'inspect', subagent_type: 'Explore' } as any);
  const ui = await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} as any });
  expect(JSON.stringify(await ui.drawn())).not.toContain(SPAWNING);
  expect(JSON.stringify(await ui.drawn())).toContain('unity gateway smart router');
  expect((await $.prompt.submit({ text: 'task', wait: false, origin: { kind: 'composer' } })).context).toBeUndefined();
  await ui.unmount();
});

test('headless sessions retain the ordinary banner without reporting instructions', { options: OPTIONS }, async ($, on) => {
  setup(on);
  await $.session.start({ surface: 'terminal', isInteractive: false, cwd: '/work' });
  const prompt = await $.prompt.submit({ text: 'task', wait: false, origin: { kind: 'sdk' } });
  expect(prompt.context).toBeUndefined();
});
