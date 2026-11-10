import { expect, mock, test } from 'claude-code/testing';

const config = {
  model: 'ug-auto', mainModel: 'opus', storeKey: 'workspace-main',
  models: [
    {model: 'opus', label: 'Opus 5.5'}, {model: 'fable-old', label: 'Fable 5.0'},
    {model: 'fable', label: 'Fable 5.1'}, {model: 'astra', label: 'Astra 6.0'},
  ],
};
const options = {
  ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '0', unset: [], recipe: 'task_v3',
  autoPicker: JSON.stringify(config),
};
const start = {surface: 'terminal', isInteractive: true, cwd: '/work'} as const;

function host(on: any, selected = 'ug-auto') {
  const state = {selected, saved: undefined as unknown, ask: 'Astra 6.0', questions: [] as any[], closed: [] as any[], calls: [] as any[], forwarded: [] as any[], tails: [] as any[]};
  const env: Record<string, string | undefined> = {
    ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: '0', UCODE_SMART_ROUTER_PYTHON: '/ug/python',
    UCODE_SESSION_ENV_FILE: '/session/env.json',
  };
  const clock = mock.clock(on);
  on('env.get', ($: any, e: any) => ({value: env[e.name]}));
  on('env.set', ($: any, e: any) => {env[e.name] = e.value; return {value: undefined};});
  on('fs.read', () => ({value: '{}'}));
  on('session.model', () => ({value: state.selected}));
  on('session.start', () => ({cwd: '/work'}));
  mock.session(on);
  on('command.register', () => ({value: {}}));
  on('store.get', () => ({value: state.saved}));
  on('store.set', ($: any, e: any) => {state.saved = e.value; return {value: undefined};});
  on('process.run', ($: any, e: any) => {state.calls.push(e); return {value: {exitCode: 0, stdout: '', stderr: ''}};});
  on('ui.open', ($: any, e: any) => { state.questions.push(e); return {value: {isPlaced: true}}; });
  on('ui.close', ($: any, e: any) => {state.closed.push(e); return {value: undefined};});
  on('agent.list', () => ({value: []}));
  on('ui.toast', () => ({value: undefined}));
  on('ui.render', {component: 'PromptHint'}, ($: any, e: any) => {
    state.tails.push(e.props.tail);
    return $.ui.resolve(e).Text({children: [e.props.tail ?? '']});
  });
  on('turn.step', async function* ($: any, e: any) {
    state.forwarded.push(e);
    return {turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null};
  });
  return {state, env, clock};
}

async function step($: any, agentId?: string) {
  const stream = $.turn.step({turnId: 'turn-1', index: 1, model: 'native', messageCount: 1, agentId});
  while (!(await stream.next()).done) {}
}

async function hint($: any, state: any) {
  const mounted = await $.ui.mount({plugin: 'ug-smart-router', component: 'PromptHint', surface: 'terminal', props: {tail: 'native'}});
  await mounted.unmount();
  return state.tails.at(-1);
}

test('Auto sends the main model and retains the native model display with a separate indicator', {options}, async ($, on) => {
  const {state} = host(on);
  await $.session.start(start);
  await step($);
  expect(state.forwarded[0].model).toBe('opus');
  expect(state.selected).toBe('ug-auto');
  expect(await hint($, state)).toBe('native · Auto(Opus 5.5)');
  await step($, 'child');
  expect(state.forwarded[1].model).toBe('native');
});

test('/auto changes only the main preference and offers the complete catalog', {options}, async ($, on) => {
  const {state, env} = host(on, 'opus');
  await $.session.start(start);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  await $.command.run({command: 'auto'});
  expect(state.questions[0].id).toBe('auto-main');
  const pane = await $.ui.mount({plugin: 'ug-smart-router', component: 'Pane', requestId: 'auto-main', surface: 'terminal', props: {} as any});
  await pane.select({key: 'auto-main', value: 'astra'});
  await pane.unmount();
  expect(state.saved).toBe('astra');
  expect(state.selected).toBe('opus');
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(await hint($, state)).toBe('native');
  state.selected = 'ug-auto';
  await step($);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(state.forwarded[0].model).toBe('astra');
  expect(await hint($, state)).toBe('native · Auto(Astra 6.0)');
});

test('cancellation and a removed saved model preserve a valid default', {options}, async ($, on) => {
  const {state} = host(on);
  state.saved = 'removed';
  state.ask = 'dismiss';
  await $.command.run({command: 'auto'});
  expect(state.saved).toBe('removed');
  await step($);
  expect(state.forwarded[0].model).toBe('opus');
});

test('reselecting Auto opens the next step and manual selection disables routing', {options}, async ($, on) => {
  const {state, env, clock} = host(on);
  const select = async (label: string) => {
    await $.session.append({door: 'command', uuid: label, message: {type: 'user', content: [{type: 'text', text: `<local-command-stdout>Set model to \`${label}\`</local-command-stdout>`}]}});
    await clock.advance(0);
  };
  await select('Auto');
  expect(state.saved).toBeUndefined();
  expect(state.questions.length).toBe(1);
  state.selected = 'opus';
  await select('Opus');
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(state.questions.length).toBe(1);
  expect(await hint($, state)).toBe('native');
});

test('native model panel places Auto before its transcript and dismisses it on cancellation', {options}, async ($, on) => {
  const {state, clock} = host(on);
  on('command.run', {command: 'model'}, () => ({ref: 1}));
  const confirm = async (text: string) => {
    await $.session.append({door: 'command', uuid: text, message: {type: 'user', content: [{type: 'text', text: `<local-command-stdout>${text}</local-command-stdout>`}]}});
    await clock.advance(0);
  };
  await $.command.run({command: 'model', args: ''});
  expect(state.questions.length).toBe(1);
  expect(state.questions[0].id).toBe('auto-main');
  await confirm('Set model to Auto for this session only');
  expect(state.questions.length).toBe(2);
  expect(state.closed.length).toBe(0);
  await $.command.run({command: 'model', args: ''});
  await confirm('Kept model as Auto');
  expect(state.closed[0].id).toBe('auto-main');
  state.selected = 'opus';
  await $.command.run({command: 'model', args: ''});
  await confirm('Set model to Opus');
  expect(state.questions.length).toBe(4);
  expect(state.closed.length).toBe(2);
});

test('direct model commands open the main selector only for Auto', {options}, async ($, on) => {
  const {state} = host(on, 'opus');
  on('command.run', {command: 'model'}, () => ({text: 'Set model'}));
  await $.command.run({command: 'model', args: 'opus'});
  expect(state.questions.length).toBe(0);
  state.selected = 'ug-auto';
  await $.command.run({command: 'model', args: 'ug-auto'});
  expect(state.questions.length).toBe(1);
  expect(state.questions[0].id).toBe('auto-main');
});
