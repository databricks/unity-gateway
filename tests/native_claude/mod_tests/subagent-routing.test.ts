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
// One block of the parent's reply: Claude calls the hook once per block, and every block of
// a message shares its requestId.
const reply = (id: string) => ({
  ...panel, component: 'AssistantMessage' as const, requestId: id,
  props: { text: 'reply', isFirstOfReply: true },
});
test('a lone call keeps its decision under its own row and draws no card', async ($, on) => {
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

  for (const [id, model] of [['call-a', 'claude-sonnet-5'], ['call-b', 'claude-opus-4-8']]) {
    const done = await $.ui.mount(row(id));
    expect(await done.find({ type: 'Text', text: 'original Agent row' })).toBeDefined();
    expect(await done.find({ type: 'Text', text: '  ↳ Selected Model: ' })).toBeDefined();
    expect(await done.find({ type: 'Text', text: model })).toBeDefined();
    expect(await done.find({ type: 'Text', text: new RegExp(`Selected for ${id}`) })).toBeDefined();
    expect(await done.find({ type: 'Text', text: /ug-smart-router:|�/ })).toBeUndefined();
    expect(JSON.stringify(await done.drawn())).toContain('"color":"green"');
    await done.unmount();
  }
  // Each reply block, whatever its message, draws no card for a call its row already shows.
  for (const block of [reply('first'), reply('first'), reply('second')]) {
    const ui = await $.ui.mount(block);
    expect(await ui.find({ type: 'Text', text: /Selected Model/ })).toBeUndefined();
    await ui.unmount();
  }
  const prompt = await $.ui.mount(panel);
  expect(await prompt.find({ type: 'Text', text: /Selected Model|claude-/ })).toBeUndefined();
  await prompt.unmount();
});

test('parallel cards keep call order, skip replies drawn earlier, and wait for routes still running', async ($, on) => {
  const releases = new Map<string, () => void>();
  let started: () => void = () => {};
  const ready = new Promise<void>((resolve) => { started = resolve; });
  const events: string[] = [];
  on('clock.now', () => ({ value: 10_000 }));
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', async ($, e) => {
    const { tool_use_id, tool_input } = JSON.parse(e.init.stdin);
    await new Promise<void>((resolve) => {
      releases.set(tool_use_id, resolve);
      if (releases.size === 3) started();
    });
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: tool_use_id === 'call-a' ? 'system.ai.claude-sonnet-5' : 'system.ai.claude-opus-4-8',
      rationale: `Selected for ${tool_input.description}`,
      updatedInput: { ...tool_input, subagent_type: `ug-smart-router:${tool_use_id}` },
    }) } };
  });
  on('tool.call', ($, e) => {
    expect(e.run_in_background).toBe(true);
    events.push(`start ${e.tool_use_id}`);
    return { result: 'ok' };
  });
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['other row'] }));
  const input = { ...originalInput, run_in_background: true };
  const before = await $.ui.mount(reply('before'));
  await before.unmount();
  const first = $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...input, description: 'Compute 1+1 (agent A)' });
  const second = $.tool.call({ tool: 'Task', tool_use_id: 'call-b', ...input, description: 'Compute 1+1 (agent B)' });
  const third = $.tool.call({ tool: 'Agent', tool_use_id: 'call-c', ...input, description: 'Compute 1+1 (agent C)' });
  await ready;
  releases.get('call-b')!();
  await second;
  releases.get('call-a')!();
  await first;
  expect(events).toEqual(['start call-b', 'start call-a']);

  // A reply drawn before routing is never an anchor.
  const old = await $.ui.mount(reply('before'));
  expect(await old.find({ type: 'Text', text: /Selected Model/ })).toBeUndefined();
  await old.unmount();
  const cards = await $.ui.mount(reply('after'));
  const tree = JSON.stringify(await cards.drawn());
  expect(tree.indexOf('Compute 1+1 (agent A)')).toBeGreaterThan(-1);
  expect(tree.indexOf('Compute 1+1 (agent A)')).toBeLessThan(tree.indexOf('Compute 1+1 (agent B)'));
  expect(tree.indexOf('Compute 1+1 (agent B)')).toBeLessThan(tree.indexOf('other row'));
  expect(tree).not.toContain('agent C');
  expect(await cards.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: 'claude-opus-4-8' })).toBeDefined();
  await cards.unmount();

  releases.get('call-c')!();
  await third;
  const next = await $.ui.mount(reply('next'));
  expect(await next.find({ type: 'Text', text: 'Compute 1+1 (agent C)' })).toBeDefined();
  expect(await next.find({ type: 'Text', text: 'Compute 1+1 (agent A)' })).toBeUndefined();
  await next.unmount();
  const prompt = await $.ui.mount(panel);
  expect(await prompt.find({ type: 'Text', text: /Selected Model|claude-/ })).toBeUndefined();
  await prompt.unmount();
});

test('an aborted turn leaves no card for the next turn, but a finished one keeps them for the reply', async ($, on) => {
  const gates = new Map<string, () => void>();
  let started: () => void = () => {};
  const bothStarted = new Promise<void>((resolve) => { started = resolve; });
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', async ($, e) => {
    const { tool_use_id, tool_input } = JSON.parse(e.init.stdin);
    await new Promise<void>((resolve) => {
      gates.set(tool_use_id, resolve);
      if (gates.size === 2) started();
    });
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.claude-sonnet-5', rationale: `Selected for ${tool_use_id}`,
      updatedInput: { ...tool_input, subagent_type: `ug-smart-router:${tool_use_id}` },
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: '' }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['other row'] }));
  const parallel = async (ids: string[]) => {
    gates.clear()
    const calls = ids.map((id) => $.tool.call({ tool: 'Agent', tool_use_id: id, ...originalInput, description: `Inspect ${id}` }));
    await bothStarted;
    for (const id of ids) gates.get(id)!();
    await Promise.all(calls);
  };

  // The user interrupts the turn after its parallel calls were routed.
  await $.prompt.submit({ text: 'inspect the parser' });
  await parallel(['call-a', 'call-b']);
  await $.turn.complete({ answer: '', isAborted: true });
  await $.prompt.submit({ text: 'something unrelated' });
  const unrelated = await $.ui.mount(reply('unrelated'));
  expect(await unrelated.find({ type: 'Text', text: /Selected Model|call-a|call-b/ })).toBeUndefined();
  await unrelated.unmount();
});

test('a turn that ends while the reply waits on a background agent keeps its cards for that reply', async ($, on) => {
  const gates = new Map<string, () => void>();
  let started: () => void = () => {};
  const bothStarted = new Promise<void>((resolve) => { started = resolve; });
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', async ($, e) => {
    const { tool_use_id, tool_input } = JSON.parse(e.init.stdin);
    await new Promise<void>((resolve) => {
      gates.set(tool_use_id, resolve);
      if (gates.size === 2) started();
    });
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.claude-sonnet-5', rationale: `Selected for ${tool_use_id}`,
      updatedInput: { ...tool_input, subagent_type: `ug-smart-router:${tool_use_id}` },
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('turn.complete', () => ({ text: '' }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['other row'] }));

  // The message that issued the calls is drawn before they are routed, in two blocks.
  for (let block = 0; block < 2; block++) {
    const issuing = await $.ui.mount(reply('issuing'));
    await issuing.unmount();
  }
  const calls = ['call-a', 'call-b'].map((id) => $.tool.call({ tool: 'Agent', tool_use_id: id, ...originalInput, run_in_background: true, description: `Inspect ${id}` }));
  await bothStarted;
  for (const gate of gates.values()) gate();
  await Promise.all(calls);
  await $.turn.complete({ answer: 'The subagents are running.', isAborted: false });

  const issuing = await $.ui.mount(reply('issuing'));
  expect(await issuing.find({ type: 'Text', text: /Selected Model/ })).toBeUndefined();
  await issuing.unmount();
  const final = await $.ui.mount(reply('final'));
  expect(await final.findAll({ type: 'Text', text: 'claude-sonnet-5' })).toHaveLength(2);
  expect(await final.find({ type: 'Text', text: 'Inspect call-a' })).toBeDefined();
  expect(await final.find({ type: 'Text', text: 'Inspect call-b' })).toBeDefined();
  await final.unmount();
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
    for (const mount of [row('call-a'), reply('after')]) {
      const ui = await $.ui.mount(mount);
      expect(await ui.find({ type: 'Text', text: 'original Agent row' })).toBeDefined();
      expect(await ui.find({ type: 'Text', text: /Reason:|Selected Model/ })).toBeUndefined();
      await ui.unmount();
    }
  });
}

test('a waiting call shows a spinner on its row and its card keeps the routing time', async ($, on) => {
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
  let seenSpinner: any;
  on('ui.render', ($, e) => {
    if (e.component !== 'Spinner') return { type: 'Text', props: {}, children: ['original Agent row'] };
    seenSpinner = e.props;
    return { type: 'Text', props: {}, children: [e.props.message ?? e.props.word] };
  });
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
  // The turn's spinner line counts the calls still routing, timed from the oldest.
  const spinner = { ...panel, component: 'Spinner' as const, requestId: 'spinner', props: { word: 'Genieing', message: null, suffix: '…', mode: 'tool-use' as const } };
  const line = await $.ui.mount(spinner);
  expect(await line.find({ type: 'Text', text: 'Selecting models for 2 agents… 2s' })).toBeDefined();
  expect(seenSpinner.suffix).toBe('');
  await line.unmount();
  now += 700;
  release(undefined);
  await call;
  await parallel;
  const done = await $.ui.mount(row('call-a'));
  expect(await done.find({ type: 'Text', text: /Selecting a model|Selected Model/ })).toBeUndefined();
  await done.unmount();
  const idle = await $.ui.mount(spinner);
  expect(await idle.find({ type: 'Text', text: 'Genieing' })).toBeDefined();
  await idle.unmount();
  const cards = await $.ui.mount(reply('after'));
  expect(await cards.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: ' (routed in 3.2s)' })).toBeDefined();
  await cards.unmount();
});
