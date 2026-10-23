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
  const notices: string[] = [];
  on('ui.log', ($, e) => { notices.push(e.text); return { value: undefined }; });
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

  expect(notices.length).toBe(2);
  expect(notices[0]).toContain('Smart Router: Inspect parser [call-a]');
  expect(notices[0]).toContain('↳ Selected Model: claude-sonnet-5');
  expect(notices[0]).toContain('Reason: Selected for call-a');
  expect(notices[0]).not.toContain('claude-opus-4-8');
  expect(notices[1]).toContain('Smart Router: Inspect parser [call-b]');
  expect(notices[1]).toContain('↳ Selected Model: claude-opus-4-8');
  expect(notices[1]).toContain('Reason: Selected for call-b');
  const done = await $.ui.mount(row('call-a'));
  expect(await done.find({ type: 'Text', text: 'original Agent row' })).toBeDefined();
  expect(await done.find({ type: 'Text', text: /Selected Model|Reason:/ })).toBeUndefined();
  await done.unmount();
});

test('parallel background agents emit distinct notices without any tool render events', async ($, on) => {
  const releases = new Map<string, () => void>();
  let started: () => void = () => {};
  const ready = new Promise<void>((resolve) => { started = resolve; });
  const notices: string[] = [];
  const forwarded: string[] = [];
  on('ui.log', ($, e) => { notices.push(e.text); return { value: undefined }; });
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
      rationale: `Selected for ${tool_use_id}`,
      updatedInput: { ...tool_input, subagent_type: `ug-smart-router:${tool_use_id}` },
    }) } };
  });
  on('tool.call', ($, e) => {
    expect(notices[notices.length - 1]).toContain(`[${e.tool_use_id}]`);
    expect(e.run_in_background).toBe(true);
    forwarded.push(e.tool_use_id);
    return { result: 'ok' };
  });

  const input = { ...originalInput, run_in_background: true };
  const first = $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...input, description: 'Compute 1+1 (agent A)' });
  const second = $.tool.call({ tool: 'Task', tool_use_id: 'call-b', ...input, description: 'Compute 1+1 (agent B)' });
  await ready;
  expect(notices.length).toBe(0);
  releases.get('call-b')!();
  await second;
  expect(notices.length).toBe(1);
  expect(notices[0]).toContain('Compute 1+1 (agent B) [call-b]');
  expect(notices[0]).toContain('Selected Model: claude-opus-4-8');
  expect(notices[0]).toContain('Reason: Selected for call-b');
  expect(notices[0]).not.toContain('call-a');
  releases.get('call-a')!();
  await first;
  expect(notices.length).toBe(2);
  expect(notices[1]).toContain('Compute 1+1 (agent A) [call-a]');
  expect(notices[1]).toContain('Selected Model: claude-sonnet-5');
  expect(notices[1]).toContain('Reason: Selected for call-a');
  expect(notices[1]).not.toContain('call-b');
  expect(forwarded).toEqual(['call-b', 'call-a']);
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
  const notices: string[] = [];
  on('ui.log', ($, e) => { notices.push(e.text); return { value: undefined }; });
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
  expect(await done.find({ type: 'Text', text: /Selected Model/ })).toBeUndefined();
  expect(notices.length).toBe(2);
  expect(notices[0]).toContain('claude-sonnet-5 (routed in 3.2s)');
  await done.unmount();
});
