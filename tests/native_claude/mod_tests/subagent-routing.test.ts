import { expect, test } from 'claude-code/testing';

const command = ['/bin/ug', 'claude-router-hook', 'route-subagent-mod'];
const originalInput = { prompt: 'inspect the parser', description: 'Inspect parser', subagent_type: 'Explore', model: 'sonnet' };
const panel = {
  plugin: 'ug-smart-router', component: 'AbovePrompt', surface: 'terminal',
  requestId: 'routing-panel', viewport: { columns: 100, rows: 30 }, props: {},
} as const;
const row = (id: string) => ({
  ...panel, component: 'ToolUse' as const, requestId: id,
  props: { tool_use_id: id, tool: 'Agent', input: originalInput, isRunning: true, isErrored: false, isInterrupted: false },
});

test('routing preserves distinct results for identical tasks and forwards each call once without logging', async ($, on) => {
  const calls: any[] = [];
  const logs: string[] = [];
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
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
  expect(logs).toEqual([]);

  for (const columns of [40, 100]) {
    const cards = await $.ui.mount({ ...panel, viewport: { columns, rows: 30 } });
    expect(await cards.find({ type: 'Text', text: 'Inspect parser' })).toBeDefined();
    expect(await cards.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
    expect(await cards.find({ type: 'Text', text: 'claude-opus-4-8' })).toBeDefined();
    expect(await cards.find({ type: 'Text', text: /Selected for call-a/ })).toBeDefined();
    expect(await cards.find({ type: 'Text', text: /Selected for call-b/ })).toBeDefined();
    expect(await cards.find({ type: 'Text', text: /ug-smart-router:|\[call-|�/ })).toBeUndefined();
    expect(JSON.stringify(await cards.drawn())).toContain('"color":"green"');
    await cards.unmount();
  }
  const done = await $.ui.mount(row('call-a'));
  expect(await done.find({ type: 'Text', text: 'original Agent row' })).toBeDefined();
  expect(await done.find({ type: 'Text', text: /Selected Model|Reason:/ })).toBeUndefined();
  await done.unmount();
});

test('parallel background routes remain task-labelled without tool render events', async ($, on) => {
  const releases = new Map<string, () => void>();
  let started: () => void = () => {};
  const ready = new Promise<void>((resolve) => { started = resolve; });
  const logs: string[] = [];
  const forwarded: string[] = [];
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
  on('clock.now', () => ({ value: 10_000 }));
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', async ($, e) => {
    const { tool_use_id, tool_input } = JSON.parse(e.init.stdin);
    await new Promise<void>((resolve) => {
      releases.set(tool_use_id, resolve);
      if (releases.size === 2) started();
    });
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: tool_use_id === 'call-a' ? 'system.ai.claude-sonnet-5' : 'system.ai.claude-opus-4-8',
      rationale: `Selected for ${tool_input.description}`,
      updatedInput: { ...tool_input, subagent_type: `ug-smart-router:${tool_use_id}` },
    }) } };
  });
  on('tool.call', ($, e) => {
    expect(e.run_in_background).toBe(true);
    forwarded.push(e.tool_use_id);
    return { result: 'ok' };
  });
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['other mod'] }));
  const input = { ...originalInput, run_in_background: true };
  const first = $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...input, description: 'Compute 1+1 (agent A)' });
  const second = $.tool.call({ tool: 'Task', tool_use_id: 'call-b', ...input, description: 'Compute 1+1 (agent B)' });
  await ready;
  releases.get('call-b')!();
  await second;
  const partial = await $.ui.mount(panel);
  expect(await partial.find({ type: 'Text', text: 'Compute 1+1 (agent B)' })).toBeDefined();
  expect(await partial.find({ type: 'Text', text: 'claude-opus-4-8' })).toBeDefined();
  expect(await partial.find({ type: 'Text', text: /Selected for Compute 1\+1 \(agent B\)/ })).toBeDefined();
  expect(await partial.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeUndefined();
  await partial.unmount();
  releases.get('call-a')!();
  await first;
  const cards = await $.ui.mount(panel);
  expect(await cards.find({ type: 'Text', text: 'Compute 1+1 (agent A)' })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: /Selected for Compute 1\+1 \(agent A\)/ })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: 'claude-opus-4-8' })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: 'other mod' })).toBeDefined();
  expect(logs).toEqual([]);
  expect(forwarded).toEqual(['call-b', 'call-a']);
  await cards.unmount();
});

test('cards persist after the turn and clear on the next prompt without late routes returning', async ($, on) => {
  let started: () => void = () => {};
  let release: () => void = () => {};
  const ready = new Promise<void>((resolve) => { started = resolve; });
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', async ($, e) => {
    const { tool_use_id } = JSON.parse(e.init.stdin);
    if (tool_use_id === 'late') { started(); await waiting; }
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.claude-haiku-5-5', rationale: '',
      updatedInput: { subagent_type: 'ug-smart-router:haiku', prompt: 'inspect' },
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: '' }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['other mod'] }));
  await $.prompt.submit({ text: 'first prompt' });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'first', prompt: 'inspect', subagent_type: 'Explore' });
  await $.turn.complete({ answer: 'done' });
  const completed = await $.ui.mount(panel);
  expect(await completed.find({ type: 'Text', text: 'claude-haiku-5-5' })).toBeDefined();
  expect(await completed.find({ type: 'Text', text: 'Explore' })).toBeDefined();
  expect(await completed.find({ type: 'Text', text: /Reason:| · / })).toBeUndefined();
  await completed.unmount();
  const late = $.tool.call({ tool: 'Agent', tool_use_id: 'late', ...originalInput });
  await ready;
  await $.prompt.submit({ text: 'next prompt' });
  release();
  await late;
  const cleared = await $.ui.mount(panel);
  expect(await cleared.find({ type: 'Text', text: /Selected Model|claude-haiku/ })).toBeUndefined();
  expect(await cleared.find({ type: 'Text', text: 'other mod' })).toBeDefined();
  await cleared.unmount();
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
    const ui = await $.ui.mount(panel);
    expect(await ui.find({ type: 'Text', text: 'original Agent row' })).toBeDefined();
    expect(await ui.find({ type: 'Text', text: /Reason:|Selected Model/ })).toBeUndefined();
    await ui.unmount();
  });
}

test('a waiting call shows a spinner and the completed card retains its routing time', async ($, on) => {
  let release: (value: unknown) => void = () => {};
  const routed = new Promise((resolve) => { release = resolve; });
  let now = 10_000;
  on('clock.now', () => ({ value: now }));
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', async () => {
    await routed;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.claude-sonnet-5', rationale: 'Small task',
      updatedInput: { prompt: originalInput.prompt, subagent_type: 'ug-smart-router:haiku' },
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));
  const call = $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...originalInput });
  const waiting = await $.ui.mount(row('call-a'));
  expect(await waiting.find({ type: 'Text', text: 'Selecting a model… 0s' })).toBeDefined();
  await waiting.unmount();
  now += 2_500;
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
  expect(await done.find({ type: 'Text', text: /Selecting a model|Selected Model/ })).toBeUndefined();
  await done.unmount();
  const cards = await $.ui.mount(panel);
  expect(await cards.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: ' · 3.2s' })).toBeDefined();
  await cards.unmount();
});
