import { expect, test } from 'claude-code/testing';

const command = ['/bin/ug', 'claude-router-hook', 'route-subagent-mod'];
const originalInput = { prompt: 'inspect the parser', description: 'Inspect parser', subagent_type: 'Explore', model: 'sonnet' };

const row = (id: string) => ({
  plugin: 'ug-smart-router',
  component: 'ToolUse' as const,
  surface: 'terminal' as const,
  requestId: id,
  viewport: { columns: 100, rows: 30 },
  props: { tool_use_id: id, tool: 'Agent', input: originalInput, isRunning: true, isErrored: false, isInterrupted: false },
});

test('routing preserves distinct results for identical tasks and forwards each call once', async ($, on) => {
  const calls: any[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', ($, e) => {
    expect(e.argv).toEqual(command);
    expect(e.init.timeoutMs).toBe(35000);
    const payload = JSON.parse(e.init.stdin);
    expect(payload.session_id).toBe('session-1');
    expect(payload.tool_input).toEqual(originalInput);
    const model = payload.tool_use_id === 'call-a' ? 'system.ai.claude-sonnet-5' : 'system.ai.claude-opus-4-8';
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model, rationale: `Selected for ${payload.tool_use_id}`,
      updatedInput: { prompt: originalInput.prompt, description: originalInput.description, subagent_type: `ug-smart-router:${payload.tool_use_id}` },
    }) } };
  });
  on('tool.call', ($, e) => { calls.push(e); return { result: 'ok' }; });
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

  await $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...originalInput });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'call-b', ...originalInput });
  expect(calls.length).toBe(2);
  expect(calls[0].tool_use_id).toBe('call-a');
  expect(calls[0].subagent_type).toBe('ug-smart-router:call-a');
  expect(calls[0].model).toBeUndefined();
  expect(calls[1].subagent_type).toBe('ug-smart-router:call-b');

  const a = await $.ui.mount(row('call-a'));
  expect(await a.find({ type: 'Text', text: 'original Agent row' })).toBeDefined();
  expect(await a.find({ type: 'Text', text: '  ↳ Selected Model: ' })).toBeDefined();
  expect(await a.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
  expect(await a.find({ type: 'Text', text: 'Reason: ' })).toBeDefined();
  expect(await a.find({ type: 'Text', text: /Selected for call-a/ })).toBeDefined();
  expect(await a.find({ type: 'Text', text: 'claude-opus-4-8' })).toBeUndefined();
  // UG's routed agent name only repeats the model, and the requested type is never shown.
  expect(await a.find({ type: 'Text', text: /ug-smart-router:/ })).toBeUndefined();
  expect(await a.find({ type: 'Text', text: /Explore/ })).toBeUndefined();
  await a.unmount();
  const b = await $.ui.mount(row('call-b'));
  expect(await b.find({ type: 'Text', text: 'claude-opus-4-8' })).toBeDefined();
  expect(await b.find({ type: 'Text', text: /Selected for call-b/ })).toBeDefined();
  expect(await b.find({ type: 'Text', text: /ug-smart-router:/ })).toBeUndefined();
  await b.unmount();
});

test('collapsed parallel agents retain task-labelled routing while finishing out of order', async ($, on) => {
  const releases = new Map<string, () => void>();
  const calls = ['call-a', 'call-b', 'call-unrouted'].map((id) => ({
    ...row(id).props,
    input: { ...originalInput, description: `Task ${id}` },
  }));
  const group = {
    plugin: 'ug-smart-router',
    component: 'ToolGroup' as const,
    surface: 'terminal' as const,
    requestId: 'group-not-a-tool-id',
    viewport: { columns: 100, rows: 30 },
    props: { calls, isExpanded: false, isActive: true, isCallsOnly: true },
  };
  on('clock.now', () => ({ value: 10_000 }));
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', async ($, e) => {
    const { tool_use_id } = JSON.parse(e.init.stdin);
    await new Promise<void>((resolve) => { releases.set(tool_use_id, resolve); });
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: tool_use_id === 'call-a' ? 'system.ai.claude-sonnet-5' : 'system.ai.claude-opus-4-8',
      rationale: `Selected for ${tool_use_id}`,
      updatedInput: { ...originalInput, subagent_type: `ug-smart-router:${tool_use_id}` },
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original group'] }));

  const first = $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...originalInput });
  const second = $.tool.call({ tool: 'Task', tool_use_id: 'call-b', ...originalInput });
  const pending = await $.ui.mount(group);
  expect(await pending.find({ type: 'Text', text: '  Task call-a' })).toBeDefined();
  expect(await pending.find({ type: 'Text', text: '  Task call-b' })).toBeDefined();
  expect(await pending.find({ type: 'Text', text: /Selecting a model/ })).toBeDefined();
  await pending.unmount();

  releases.get('call-b')!();
  await second;
  const partial = await $.ui.mount(group);
  expect(await partial.find({ type: 'Text', text: /Selecting a model/ })).toBeDefined();
  expect(await partial.find({ type: 'Text', text: 'claude-opus-4-8' })).toBeDefined();
  expect(await partial.find({ type: 'Text', text: /Selected for call-b/ })).toBeDefined();
  expect(await partial.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeUndefined();
  await partial.unmount();

  releases.get('call-a')!();
  await first;
  const done = await $.ui.mount({ ...group, props: { ...group.props, isActive: false } });
  expect(await done.find({ type: 'Text', text: 'original group' })).toBeDefined();
  for (const id of ['call-a', 'call-b']) {
    expect(await done.find({ type: 'Text', text: `  Task ${id}` })).toBeDefined();
    expect(await done.find({ type: 'Text', text: `Selected for ${id}` })).toBeDefined();
  }
  expect(await done.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
  expect(await done.find({ type: 'Text', text: 'claude-opus-4-8' })).toBeDefined();
  expect(await done.find({ type: 'Text', text: /Selecting a model|Task call-unrouted/ })).toBeUndefined();
  await done.unmount();

  for (const [index, model, otherModel] of [
    [0, 'claude-sonnet-5', 'claude-opus-4-8'],
    [1, 'claude-opus-4-8', 'claude-sonnet-5'],
  ] as const) {
    const isolated = await $.ui.mount({ ...group, props: { ...group.props, calls: [calls[index]] } });
    expect(await isolated.find({ type: 'Text', text: model })).toBeDefined();
    expect(await isolated.find({ type: 'Text', text: `Selected for ${calls[index].tool_use_id}` })).toBeDefined();
    expect(await isolated.find({ type: 'Text', text: otherModel })).toBeUndefined();
    await isolated.unmount();
  }

  for (const props of [
    { ...group.props, isExpanded: true },
    { ...group.props, calls: [calls[2]] },
  ]) {
    const unchanged = await $.ui.mount({ ...group, props });
    expect(await unchanged.find({ type: 'Text', text: 'original group' })).toBeDefined();
    expect(await unchanged.find({ type: 'Text', text: /Selected Model|Selected for/ })).toBeUndefined();
    await unchanged.unmount();
  }
});

for (const [name, result, warningCount] of [
  ['disabled', { value: { exitCode: 0, stdout: '', stderr: '' } }, 0],
  ['command failure', { value: { exitCode: 1, stdout: '', stderr: 'failure' } }, 1],
  ['invalid JSON', { value: { exitCode: 0, stdout: 'invalid', stderr: '' } }, 1],
  ['invalid response', { value: { exitCode: 0, stdout: '{}', stderr: '' } }, 1],
  ['process unavailable', { deny: 'process unavailable' }, 1],
] as const) {
  test(`${name} forwards the original call without a routing annotation`, async ($, on) => {
    const calls: any[] = [];
    const warnings: string[] = [];
    on('session.id', () => ({ value: 'session-1' }));
    on('process.run', () => result);
    on('ui.log', ($, e) => { warnings.push(e.text); return { value: undefined }; });
    on('tool.call', ($, e) => { calls.push(e); return { deny: 'user policy' }; });
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

    const answer = await $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...originalInput });
    expect(answer).toEqual({ deny: 'user policy' });
    expect(calls.length).toBe(1);
    expect(calls[0].subagent_type).toBe(originalInput.subagent_type);
    expect(calls[0].model).toBe(originalInput.model);
    expect(warnings.length).toBe(warningCount);
    const ui = await $.ui.mount(row('call-a'));
    expect(await ui.find({ type: 'Text', text: 'original Agent row' })).toBeDefined();
    expect(await ui.find({ type: 'Text', text: /Reason:/ })).toBeUndefined();
    expect(await ui.find({ type: 'Text', text: /Selected Model/ })).toBeUndefined();
    await ui.unmount();
  });
}

test('a call waiting on the router shows a spinner until its model is selected', async ($, on) => {
  let release: (value: unknown) => void = () => {};
  const routed = new Promise((resolve) => { release = resolve; });
  let now = 10_000;
  on('clock.now', () => ({ value: now }));
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', async () => {
    await routed;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.claude-sonnet-5', rationale: 'Selected for call-a',
      updatedInput: { prompt: originalInput.prompt, subagent_type: 'ug-smart-router:call-a' },
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

  const call = $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...originalInput });
  const waiting = await $.ui.mount(row('call-a'));
  expect(await waiting.find({ type: 'Text', text: 'Selecting a model… 0s' })).toBeDefined();
  expect(await waiting.find({ type: 'Text', text: /Selected Model/ })).toBeUndefined();
  await waiting.unmount();
  // The timer counts whole seconds since routing started.
  now += 2_500;
  // A parallel call starts its own ticker, but both rows show the frame the clock gives.
  const parallel = $.tool.call({ tool: 'Agent', tool_use_id: 'call-b', ...originalInput });
  for (const id of ['call-a', 'call-b']) {
    const later = await $.ui.mount(row(id));
    expect(await later.find({ type: 'Text', text: '✽ ' })).toBeDefined();
    if (id === 'call-a') expect(await later.find({ type: 'Text', text: 'Selecting a model… 2s' })).toBeDefined();
    await later.unmount();
  }

  now += 700;
  release(undefined);
  await call;
  await parallel;
  const done = await $.ui.mount(row('call-a'));
  expect(await done.find({ type: 'Text', text: /Selecting a model/ })).toBeUndefined();
  expect(await done.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
  // The selected row keeps how long routing took.
  expect(await done.find({ type: 'Text', text: ' (routed in 3.2s)' })).toBeDefined();
  await done.unmount();
});
