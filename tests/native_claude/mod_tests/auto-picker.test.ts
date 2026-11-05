import type { On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';

const AUTO = {
  EXPOSE_AUTO_IN_MODEL_PICKER: '1',
  autoMainModel: 'admin-sonnet',
  ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1', unset: [], recipe: 'test-recipe',
  orchestratorInstructions: 'Delegate under the orchestrator.',
};

test('Auto sessions reject stale smart-router commands without forwarding', {options: AUTO}, async ($, on) => {
  let forwarded = false;
  on('command.run', {command: 'smart-router'}, () => { forwarded = true; return {}; });
  for (const args of ['on', 'off', '']) {
    expect((await $.command.run({command: 'smart-router', args})).text).toContain('/model');
  }
  expect(forwarded).toBe(false);
});

function engine(on: On, {rejectModel = false, failControls = false, referenceResult = false} = {}) {
  let model = 'user-pinned-opus';
  const commands: string[] = [];
  const env: Record<string, string | undefined> = {
    ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: '0', UCODE_SMART_ROUTER_PYTHON: '/ug/python',
    UCODE_SESSION_ENV_FILE: '/session/env.json',
  };
  on('session.model', () => ({value: model}));
  on('env.get', ($, e) => ({value: env[e.name]}));
  on('env.set', ($, e) => { env[e.name] = e.value; return {value: undefined}; });
  on('process.run', ($, e) => ({value: {
    exitCode: failControls ? 1 : 0,
    stdout: e.argv[0] === '/router' ? JSON.stringify({model: 'routed-sonnet', rationale: 'Selected for task'}) : '',
    stderr: '',
  }}));
  on('command.run', {command: 'model'}, ($, e) => {
    commands.push(e.args);
    if (!e.args) return {};
    if (rejectModel) return referenceResult ? {ref: 1} : {text: 'Model is unavailable'};
    model = e.args;
    return referenceResult ? {ref: 1} : {text: `Set model to ${model}`};
  });
  on('ui.render', {component: 'CommandOutput'}, ($, e) => $.ui.resolve(e).Text({children: [e.props.text]}));
  return {commands, env, model: () => model, setModel: (value: string) => { model = value; }};
}

for (const referenceResult of [false, true]) {
  test(`Auto applies the admin main model with ${referenceResult ? 'reference' : 'text'} results`, {options: AUTO}, async ($, on) => {
    const state = engine(on, {referenceResult});
    const result = await $.command.run({command: 'model', args: 'auto'});
    expect(result.text).toContain('Auto enabled');
    expect(state.commands).toEqual(['admin-sonnet']);
    expect(state.model()).toBe('admin-sonnet');
    expect(state.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    expect(state.env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('1');
  });
}

test('/model keeps the normal native picker and opens no custom pane', {options: AUTO}, async ($, on) => {
  on('session.model', () => ({value: 'sonnet'}));
  on('env.get', () => ({value: '0'}));
  let forwarded = false;
  let popup = false;
  on('command.run', {command: 'model'}, ($, e) => { forwarded = e.args === ''; return {}; });
  on('ui.open', () => { popup = true; return {}; });
  await $.command.run({command: 'model', args: ''});
  expect(forwarded).toBe(true);
  expect(popup).toBe(false);
});

test('Auto uses the existing smart-router footer without an Auto label', {options: AUTO}, async ($, on) => {
  mock.clock(on);
  on('env.get', ($, e) => ({value: e.name === 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY' ? '1' : undefined}));
  on('agent.list', () => ({value: []}));
  let tail;
  on('ui.render', {component: 'PromptHint'}, ($, e) => {
    tail = e.props.tail;
    return $.ui.resolve(e).Text({children: ['native hint']});
  });
  const row = await $.ui.mount({plugin: 'ug-smart-router', surface: 'terminal', component: 'PromptHint', props: {}});
  await row.drawn();
  expect(tail).toBe('smart router ON');
});


test('native Auto selection applies the admin default without opening a second picker', {options: AUTO}, async ($, on) => {
  const clock = mock.clock(on);
  const state = engine(on, {referenceResult: true});
  let assigned = false;
  on('config.set', {key: 'model'}, () => { assigned = true; return {}; });
  await $.command.run({command: 'model', args: ''});
  const result = await $.config.set({key: 'model', value: 'auto'});
  expect(result.deny).toContain('admin default');
  expect(assigned).toBe(false);
  const output = await $.ui.mount({plugin: 'ug-smart-router', surface: 'terminal', component: 'CommandOutput', props: {command: 'model', text: "Couldn't save Model: Applying Auto with the admin default main model."}});
  expect(JSON.stringify(await output.drawn())).toContain('Applying Auto');
  expect(JSON.stringify(await output.drawn())).not.toContain("Couldn't save Model");
  await output.redraw();
  expect(JSON.stringify(await output.drawn())).toContain('Applying Auto');
  expect(JSON.stringify(await output.drawn())).not.toContain("Couldn't save Model");
  await clock.advance(1);
  expect(JSON.stringify(await output.drawn())).toContain('Auto enabled with admin default main model admin-sonnet.');
  await output.redraw();
  expect(JSON.stringify(await output.drawn())).toContain('Auto enabled with admin default main model admin-sonnet.');
  expect(state.commands).toEqual(['', 'admin-sonnet']);
  expect(state.model()).toBe('admin-sonnet');
  expect(state.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(state.env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('1');
});

test('a session-only Auto selection switches directly to the admin default', {options: AUTO}, async ($, on) => {
  mock.clock(on);
  const state = engine(on);
  await $.command.run({command: 'model', args: ''});
  state.setModel('auto');
  const output = await $.ui.mount({plugin: 'ug-smart-router', surface: 'terminal', component: 'CommandOutput', props: {command: 'model', text: 'Set model to auto for this session only'}});
  await output.drawn();
  expect(state.commands).toEqual(['', 'admin-sonnet']);
  expect(state.model()).toBe('admin-sonnet');
  expect(state.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

for (const defaultModel of ['', 'auto']) {
  test(`Auto rejects invalid admin default ${JSON.stringify(defaultModel)} without changing models`, {options: {...AUTO, autoMainModel: defaultModel}}, async ($, on) => {
    const state = engine(on);
    expect((await $.command.run({command: 'model', args: 'auto'})).text).toContain('default_model');
    expect(state.commands).toEqual([]);
    expect(state.model()).toBe('user-pinned-opus');
    expect(state.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  });
}

for (const failure of ['model', 'controls']) {
  test(`Auto leaves routing off when ${failure} fails`, {options: AUTO}, async ($, on) => {
    const state = engine(on, {rejectModel: failure === 'model', failControls: failure === 'controls'});
    expect((await $.command.run({command: 'model', args: 'auto'})).text).toContain('Dispatch is paused');
    expect(state.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
    expect(state.env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('0');
  });
}

test('a concrete selection after Auto disables both activities without touching children', {options: AUTO}, async ($, on) => {
  const state = engine(on);
  await $.command.run({command: 'model', args: 'auto'});
  await $.command.run({command: 'model', args: 'admin-sonnet'});
  expect(state.model()).toBe('admin-sonnet');
  expect(state.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(state.env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('0');
  expect(state.commands).toEqual(['admin-sonnet', 'admin-sonnet']);
});

test('a native message reference cannot confirm an unchanged main model', {options: AUTO}, async ($, on) => {
  const state = engine(on, {referenceResult: true, rejectModel: true});
  expect((await $.command.run({command: 'model', args: 'auto'})).text).toContain('Dispatch is paused');
  expect(state.model()).toBe('user-pinned-opus');
  expect(state.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(state.env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('0');
});

for (const failure of ['model', 'controls']) {
  test(`a saved Auto row does not enable routing when ${failure} fails`, {options: AUTO}, async ($, on) => {
    const clock = mock.clock(on);
    const state = engine(on, {rejectModel: failure === 'model', failControls: failure === 'controls'});
    await $.command.run({command: 'model', args: ''});
    expect((await $.config.set({key: 'model', value: 'auto'})).deny).toContain('admin default');
    const output = await $.ui.mount({plugin: 'ug-smart-router', surface: 'terminal', component: 'CommandOutput', props: {command: 'model', text: 'Set model to Auto'}});
    await output.drawn();
    await clock.advance(1);
    expect(JSON.stringify(await output.drawn())).toContain('Dispatch is paused');
    await output.redraw();
    expect(JSON.stringify(await output.drawn())).toContain('Dispatch is paused');
    expect(state.commands).toEqual(['', 'admin-sonnet']);
    expect(state.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
    expect(state.env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('0');
  });
}

test('Auto changes invalidate pending spawn routes while existing children can resume', {options: {...AUTO, routeCommand: ['/router']}}, async ($, on) => {
  engine(on);
  on('session.id', () => ({value: 'session-1'}));
  on('tool.call', () => ({result: 'ok'}));
  on('agent.spawn', ($, e) => ({model: e.model ?? 'original-model', agentId: 'child-1'}));
  on('fs.write', () => ({value: undefined}));
  const call = {tool: 'Agent', tool_use_id: 'pending', prompt: 'Review the code', subagent_type: 'general-purpose'};
  const spawn = {
    tool_use_id: 'pending', prompt: call.prompt, description: 'Review',
    subagentType: call.subagent_type, parentModel: 'admin-sonnet',
    provider: {plugin: 'engine', tier: 'core' as const}, background: true, fork: false,
    name: 'reviewer', permissionMode: 'default', parentAgentId: 'parent', cwd: '/workspace',
  };
  await $.command.run({command: 'model', args: 'auto'});
  await $.tool.call(call);
  await $.command.run({command: 'model', args: 'admin-sonnet'});
  expect((await $.agent.spawn(spawn)).deny).toContain('Auto changed');
  const resumed = await $.agent.spawn({...spawn, model: 'existing-child-model', resume: {agentId: 'child-1'}});
  expect(resumed.model).toBe('existing-child-model');
  await $.command.run({command: 'model', args: 'auto'});
  expect((await $.agent.spawn(spawn)).deny).toContain('Auto changed');
  await $.tool.call({...call, tool_use_id: 'fresh'});
  expect((await $.agent.spawn({...spawn, tool_use_id: 'fresh'})).model).toBe('routed-sonnet');
});
