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

test('selected model displays the model name without a gateway alias', async ($, on) => {
  const models = [
    ['anthropic-aigw-77df06ea-system.ai.glm-5-3-flash', 'glm-5-3-flash'],
    ['anthropic-aigw-c442a48-system.ai.gpt-6-1-sol[1m]', 'gpt-6-1-sol'],
    ['main.default.bedrock-mps.us.anthropic.claude-sonnet-5-5', 'us.anthropic.claude-sonnet-5-5'],
    ['model.version', 'model.version'],
    ['plain-model[1m]', 'plain-model'],
    ['databricks-claude-sonnet', 'claude-sonnet'],
    ['anthropic-aigw-invalid-custom-model', 'anthropic-aigw-invalid-custom-model'],
  ];
  let model = '';
  on('session.id', () => ({ value: 'session-1' }));
  on('clock.every', () => ({ value: { cancel: () => {} } }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ model, rationale: '' }) } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

  for (const [index, [input, expected]] of models.entries()) {
    model = input;
    const id = `model-${index}`;
    await $.tool.call({ tool: 'Agent', tool_use_id: id, ...originalInput });
    const mounted = await $.ui.mount(row(id));
    expect(JSON.stringify(await mounted.drawn())).toContain(JSON.stringify(expected));
    await mounted.unmount();
  }
});

test('animal names stay distinct and stable without changing routed calls or row controls', async ($, on) => {
  const input = { ...originalInput, subagent_type: 'ug-smart-router:ucode-route-sonnet-hash', run_in_background: true };
  const names: string[] = [];
  const forwarded: any[] = [];
  const displayInputs: any[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', ($, event) => {
    expect(JSON.parse(event.init.stdin).tool_input).toEqual(input);
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.claude-haiku-5-5', rationale: 'Selected for the task',
    }) } };
  });
  on('tool.call', ($, event) => { forwarded.push(event); return { result: 'ok' }; });
  on('ui.render', ($, event) => {
    displayInputs.push(event.props);
    return { type: 'Text', props: {}, children: [`${event.props.tool}(${event.props.input.description}) · Backgrounded agent`] };
  });

  for (let index = 0; index < 101; index++) {
    const id = `animal-${index}`;
    await $.tool.call({ tool: 'Agent', tool_use_id: id, ...input });
    const event = { ...row(id), props: { ...row(id).props, input } };
    for (let render = 0; render < 2; render++) {
      const mounted = await $.ui.mount(event);
      const displayed = displayInputs[displayInputs.length - 1];
      const name = displayed.tool;
      expect(name).toMatch(/^[A-Z][a-z]+(?: 2)?$/);
      expect(displayed).toEqual({ ...event.props, tool: name });
      expect(event.props.input).toEqual(input);
      expect(await mounted.find({ type: 'Text', text: /Backgrounded agent/ })).toBeDefined();
      expect(await mounted.find({ type: 'Text', text: 'claude-haiku-5-5' })).toBeDefined();
      expect(await mounted.find({ type: 'Text', text: /ucode-route-|ug-smart-router:/ })).toBeUndefined();
      if (render === 0) names.push(name);
      else expect(name).toBe(names[index]);
      await mounted.unmount();
    }
    expect(forwarded[index]).toEqual({ ...input, tool: 'Agent', tool_use_id: id });
  }
  expect(new Set(names).size).toBe(101);
  expect(names.slice(0, 100).every((name) => !name.includes(' '))).toBe(true);
  expect(names[100]).toMatch(/ 2$/);
});

test('routing changes only the matching spawn model and joins its display name to the child ID', async ($, on) => {
  const writes: any[] = [];
  const forwarded: any[] = [];
  const shown = new Map<string, string>();
  let failWrite = false;
  const logs: string[] = [];
  const routedModel = 'system.ai.gpt-5-6-sol[1m]';
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => {
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: routedModel, rationale: 'Selected for the task',
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('agent.spawn', ($, e) => {
    forwarded.push(e);
    return e.name === 'denied' ? { deny: 'Permission denied' }
      : { model: 'claude-sonnet-5', agentId: `child-${e.tool_use_id}` };
  });
  on('fs.write', ($, e) => {
    if (failWrite) throw new Error('Read-only filesystem');
    writes.push(e);
    return { value: undefined };
  });
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
  on('ui.render', ($, e) => {
    shown.set(e.requestId, e.props.tool);
    return { type: 'Text', props: {}, children: [e.props.tool] };
  });
  for (const id of ['call-a', 'call-b', 'denied']) {
    await $.tool.call({ tool: 'Agent', tool_use_id: id, ...originalInput });
    const mounted = await $.ui.mount(row(id));
    await mounted.unmount();
  }
  // Reverse completion order, identical descriptions: no joining by task text or order.
  for (const id of ['call-b', 'call-a', 'denied', 'unrouted']) {
    const spawn = {
      tool_use_id: id, prompt: originalInput.prompt, description: originalInput.description,
      subagentType: originalInput.subagent_type, parentModel: 'claude-opus-4-8',
      provider: { plugin: 'engine', tier: 'core' as const }, background: true, fork: false,
      name: id === 'denied' ? 'denied' : 'caller-address', permissionMode: 'default',
      parentAgentId: 'parent', cwd: '/workspace',
    };
    const result = await $.agent.spawn(spawn);
    expect(forwarded[forwarded.length - 1]).toEqual(id === 'unrouted' ? spawn
      : { ...spawn, model: routedModel });
    expect(result).toEqual(id === 'denied' ? { deny: 'Permission denied' }
      : { model: 'claude-sonnet-5', agentId: `child-${id}` });
  }
  const launched = await $.ui.mount(row('call-a'));
  expect(await launched.find({ type: 'Text', text: 'Launched Model: claude-sonnet-5' })).toBeDefined();
  await launched.unmount();
  const denied = await $.ui.mount(row('denied'));
  expect(await denied.find({ type: 'Text', text: /Launched Model:/ })).toBeUndefined();
  await denied.unmount();
  expect(writes.length).toBe(2);
  for (const [index, id] of ['call-b', 'call-a'].entries()) {
    expect(writes[index].path.endsWith(`/agent-names/agent-child-${id}.json`)).toBe(true);
    expect(JSON.parse(writes[index].text)).toEqual({ session_id: 'session-1', name: shown.get(id) });
  }
  const resumed = { ...forwarded[0], model: 'existing-child-model', resume: { agentId: 'child-call-b' } };
  await $.agent.spawn(resumed);
  expect(forwarded[forwarded.length - 1]).toEqual(resumed);
  expect(writes.length).toBe(2);
  failWrite = true;
  const result = await $.agent.spawn(forwarded[0]);
  expect(result).toEqual({ model: 'claude-sonnet-5', agentId: 'child-call-b' });
  expect(writes.length).toBe(2);
  expect(logs).toEqual(['Could not save the subagent display name; its bottom bar will use the native label.']);
});

test('a lone call ignores extra updatedInput and keeps its decision under its own row', async ($, on) => {
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
      updatedInput: {
        ...payload.tool_input,
        prompt: 'Routed prompt',
        description: 'Routed description',
        subagent_type: `ug-smart-router:${payload.tool_use_id}`,
        model: 'malicious-model',
      },
    }) } };
  });
  on('tool.call', ($, e) => { calls.push(e); return { result: 'ok' }; });
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

  await $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...originalInput });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'call-b', ...originalInput });
  expect(calls.length).toBe(2);
  expect(calls[0].tool_use_id).toBe('call-a');
  expect(calls[0]).toEqual({ tool: 'Agent', tool_use_id: 'call-a', ...originalInput });
  expect(calls[1]).toEqual({ tool: 'Agent', tool_use_id: 'call-b', ...originalInput });
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
    const { tool_use_id } = JSON.parse(e.init.stdin);
    await new Promise<void>((resolve) => {
      gates.set(tool_use_id, resolve);
      if (gates.size === 2) started();
    });
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.claude-sonnet-5', rationale: `Selected for ${tool_use_id}`,
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
    const { tool_use_id } = JSON.parse(e.init.stdin);
    await new Promise<void>((resolve) => {
      gates.set(tool_use_id, resolve);
      if (gates.size === 2) started();
    });
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.claude-sonnet-5', rationale: `Selected for ${tool_use_id}`,
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
  ['missing model', { value: { exitCode: 0, stdout: JSON.stringify({ rationale: 'Selected' }), stderr: '' } }, 1],
  ['invalid model', { value: { exitCode: 0, stdout: JSON.stringify({ model: 42, rationale: 'Selected' }), stderr: '' } }, 1],
  ['missing rationale', { value: { exitCode: 0, stdout: JSON.stringify({ model: 'system.ai.claude-sonnet-5' }), stderr: '' } }, 1],
  ['invalid rationale', { value: { exitCode: 0, stdout: JSON.stringify({ model: 'system.ai.claude-sonnet-5', rationale: 42 }), stderr: '' } }, 1],
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
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  let seenSpinner: any;
  const seenNames = new Map<string, string>();
  on('ui.render', ($, e) => {
    if (e.component === 'ToolUse') seenNames.set(e.requestId, e.props.tool);
    if (e.component !== 'Spinner') return { type: 'Text', props: {}, children: ['original Agent row'] };
    seenSpinner = e.props;
    return { type: 'Text', props: {}, children: [e.props.message ?? e.props.word] };
  });
  const call = $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...originalInput });
  const waiting = await $.ui.mount(row('call-a'));
  const firstName = seenNames.get('call-a');
  expect(firstName).toMatch(/^[A-Z][a-z]+$/);
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
  expect(seenNames.get('call-a')).toBe(firstName);
  expect(seenNames.get('call-b')).not.toBe(firstName);
  expect(await done.find({ type: 'Text', text: /Selecting a model|Selected Model/ })).toBeUndefined();
  await done.unmount();
  const idle = await $.ui.mount(spinner);
  expect(await idle.find({ type: 'Text', text: 'Genieing' })).toBeDefined();
  await idle.unmount();
  const cards = await $.ui.mount(reply('after'));
  expect(await cards.find({ type: 'Text', text: firstName! })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: seenNames.get('call-b')! })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
  expect(await cards.find({ type: 'Text', text: ' (routed in 3.2s)' })).toBeDefined();
  await cards.unmount();
});

test('fork and resumed calls retain their native model without requesting a route', async ($, on) => {
  const calls: any[] = [];
  let routes = 0;
  on('process.run', () => { routes++; return { value: { exitCode: 0, stdout: '', stderr: '' } }; });
  on('tool.call', ($, e) => { calls.push(e); return { result: 'ok' }; });
  for (const input of [{ ...originalInput, subagent_type: 'fork' }, { ...originalInput, resume: 'child' }]) {
    const call = { tool: 'Agent', tool_use_id: 'inherited', ...input };
    await $.tool.call(call);
    expect(calls[calls.length - 1]).toEqual(call);
  }
  expect(routes).toBe(0);
});

test('a name the call gave is kept as the routed agent name', async ($, on) => {
  const forwarded: any[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
    model: 'system.ai.claude-haiku-5-5', rationale: 'Selected for the task',
  }) } }));
  on('tool.call', ($, event) => { forwarded.push(event); return { result: 'ok' }; });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'named', ...originalInput, name: 'scout' });
  expect(forwarded[0].name).toBe('scout');
});

const explanationModel = 'system.ai.model-b[1m]';
const explanation = {
  selected_model: 'model-b',
  summary: 'Model B was the best expected quality and cost trade-off.',
  task_understanding: ['Reasoning: multi step', 'Coverage: exhaustive'],
  models: [
    { id: 'model-a', name: 'Model A', selected: false, considered: true,
      reason: 'The router expected a weaker fit.', quality_estimate: 0.8, estimated_cost_usd: 0.0004,
      routing_score: 0.7, components: { quality: 0.8 }, difference_from_winner: { quality: -0.1 } },
    { id: 'model-b', name: 'Model B', selected: true, considered: true,
      reason: 'Chosen for its expected quality.', quality_estimate: 0.9, estimated_cost_usd: 0.24,
      routing_score: 0.85, components: { quality: 0.9 }, difference_from_winner: { quality: 0 } },
  ],
  evidence: { request_id: 'hidden-request', source: 'hidden-source' },
};

for (const [modelCount, customNames] of [[2, false], [20, false], [2, true]] as const) {
test(`explanations for ${modelCount} models with custom names ${customNames} preserve the route`, async ($, on) => {
  const details = customNames ? {
    ...explanation, selected_model: 'custom.catalog.code',
    models: explanation.models.map((model) => ({ ...model,
      id: model.selected ? 'custom.catalog.code' : 'custom.catalog.code[1m]',
    })),
  } : explanation;
  const routedModel = customNames ? 'anthropic-aigw-01234567-custom.catalog.code' : modelCount === 20 ? 'anthropic-aigw-01234567-system.ai.model-b[1m]' : explanationModel;
  let routeCalls = 0;
  const spawns: any[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', ($, e) => {
    routeCalls++;
    const request = JSON.parse(e.init.stdin);
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: routedModel, rationale: 'Selected for the task', explanation: {
        ...details, session_id: request.session_id, tool_use_id: request.tool_use_id,
        models: [...details.models, ...Array.from({ length: modelCount - 2 }, (_, i) => ({
          id: `alternative-${i}`, name: `Alternative ${i}`, selected: false, considered: true, reason: 'Lower expected fit.',
        }))],
      }, session_id: request.session_id, tool_use_id: request.tool_use_id,
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('agent.spawn', ($, e) => { spawns.push(e); return { model: e.model, agentId: `child-${e.tool_use_id}` }; });
  on('fs.write', () => ({ value: undefined }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

  await $.tool.call({ tool: 'Agent', tool_use_id: 'off-by-default', ...originalInput });
  const hidden = await $.ui.mount(row('off-by-default'));
  expect(JSON.stringify(await hidden.drawn())).not.toContain('Why this model?');
  await hidden.unmount();

  await $.command.run({ command: 'smart-router', args: 'explain on' });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'shown', ...originalInput });
  const shown = await $.ui.mount(row('shown'));
  expect(JSON.stringify(await shown.drawn())).toContain('Why this model?');
  await shown.press({ key: 'why-this-model' });
  await shown.unmount();

  await $.command.run({ command: 'smart-router', args: 'explain latest' });
  const pane = await $.ui.mount({ ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation' });
  const tree = JSON.stringify(await pane.drawn());
  for (const text of ['best expected quality', 'Model A', 'Model B', 'Chosen for its expected quality', 'Show numbers']) {
    expect(tree).toContain(text);
  }
  for (const text of ['hidden-request', 'hidden-source', 'session-1', 'shown']) {
    expect(tree).not.toContain(text);
  }
  expect(tree).not.toContain('Est. cost');
  await pane.select({ key: 'compare-model', value: customNames ? 'custom.catalog.code[1m]' : 'model-a' });
  expect(JSON.stringify(await pane.drawn())).toContain('The router expected a weaker fit');
  await pane.press({ key: 'numbers' });
  expect(JSON.stringify(await pane.drawn())).toContain('Est. cost: 0.000400 USD');
  await pane.unmount();

  await $.agent.spawn({ tool_use_id: 'shown', name: 'caller', prompt: 'inspect' });
  expect(spawns[0].model).toBe(routedModel);
  expect(routeCalls).toBe(2);
  await $.command.run({ command: 'smart-router', args: 'explain off' });
  const off = await $.ui.mount(row('shown'));
  expect(JSON.stringify(await off.drawn())).not.toContain('Why this model?');
  await off.unmount();
});

}

test('seven-digit gateway wrappers keep their matching explanation', async ($, on) => {
  const routedModel = 'anthropic-aigw-c442a48-system.ai.model-b';
  const spawns: any[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', ($, e) => {
    const request = JSON.parse(e.init.stdin);
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: routedModel,
      rationale: 'Selected for the task',
      explanation: { ...explanation, session_id: request.session_id, tool_use_id: request.tool_use_id },
      session_id: request.session_id,
      tool_use_id: request.tool_use_id,
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('agent.spawn', ($, e) => { spawns.push(e); return { model: e.model, agentId: 'child-seven-digit' }; });
  on('fs.write', () => ({ value: undefined }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

  await $.command.run({ command: 'smart-router', args: 'explain on' });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'seven-digit', ...originalInput });
  const shown = await $.ui.mount(row('seven-digit'));
  expect(JSON.stringify(await shown.drawn())).toContain('Why this model?');
  await shown.unmount();

  await $.command.run({ command: 'smart-router', args: 'explain latest' });
  const pane = await $.ui.mount({ ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation' });
  expect(JSON.stringify(await pane.drawn())).toContain('Model B was the best expected quality and cost trade-off.');
  await pane.unmount();

  await $.agent.spawn({ tool_use_id: 'seven-digit', prompt: 'inspect' });
  expect(spawns[0].model).toBe(routedModel);
});

for (const [name, details, diagnostic] of [
  ['missing', undefined, false],
  ['malformed', { ...explanation, selected_model: 'model-c' }, true],
  ['wrong session identity', { ...explanation, session_id: 'another-session' }, true],
  ['wrong tool identity', { ...explanation, tool_use_id: 'another-tool' }, true],
] as const) {
  test(`${name} explanation shows no details while preserving the route`, async ($, on) => {
    const logs: string[] = [];
    const routed = 'system.ai.model-b[1m]';
    const forwarded: any[] = [];
    on('session.id', () => ({ value: 'session-1' }));
    on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: routed, rationale: 'Original route reason', ...(details ? { explanation: details } : {}),
    }) } }));
    on('tool.call', () => ({ result: 'ok' }));
    on('agent.spawn', ($, e) => { forwarded.push(e); return { model: e.model }; });
    on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

    await $.tool.call({ tool: 'Agent', tool_use_id: name, ...originalInput });
    expect(logs).toEqual(diagnostic
      ? ['Smart Router returned invalid explanation details; keeping its selected model.'] : []);
    await $.command.run({ command: 'smart-router', args: 'explain latest' });
    const pane = await $.ui.mount({ ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation' });
    expect(JSON.stringify(await pane.drawn())).toContain('No routing explanation details were returned.');
    expect(JSON.stringify(await pane.drawn())).toContain('Original route reason');
    await pane.unmount();

    await $.agent.spawn({ tool_use_id: name, prompt: 'inspect' });
    expect(forwarded[0].model).toBe(routed);
  });
}

for (const mismatchedField of ['session_id', 'tool_use_id'] as const) {
  test(`a mismatched route-level ${mismatchedField} does not select a model`, async ($, on) => {
    const logs: string[] = [];
    const spawns: any[] = [];
    on('session.id', () => ({ value: 'session-1' }));
    on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.model-b',
      rationale: 'Selected for the task',
      [mismatchedField]: 'another-request',
    }) } }));
    on('tool.call', () => ({ result: 'ok' }));
    on('agent.spawn', ($, e) => { spawns.push(e); return { model: e.model ?? 'native-default' }; });
    on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });

    await $.tool.call({ tool: 'Agent', tool_use_id: 'request-1', ...originalInput });
    await $.agent.spawn({ tool_use_id: 'request-1', prompt: 'inspect' });

    expect(spawns[0].model).toBeUndefined();
    expect(logs).toEqual(['Smart Router could not route this subagent; using its original settings.']);
  });
}


test('explanation parsing failures preserve a valid routed spawn', async ($, on) => {
  const forwarded: any[] = [];
  const logs: string[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout:
    '{"model":"model-b","rationale":"Original route reason","explanation":{"nested":'
      + '['.repeat(20000) + '0' + ']'.repeat(20000) + '}}',
  } }));
  on('tool.call', ($, e) => { forwarded.push(e); return { result: 'ok' }; });
  on('agent.spawn', ($, e) => { forwarded.push(e); return { model: e.model }; });
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });

  await $.tool.call({ tool: 'Agent', tool_use_id: 'deep-metadata', ...originalInput });
  expect(forwarded[0]).toEqual({ tool: 'Agent', tool_use_id: 'deep-metadata', ...originalInput });
  await $.agent.spawn({ tool_use_id: 'deep-metadata', prompt: 'inspect' });
  expect(forwarded[1].model).toBe('model-b');
  expect(logs).toEqual(['Smart Router returned invalid explanation details; keeping its selected model.']);
});


test('the Why button reports when the explanation pane cannot be placed', async ($, on) => {
  const toasts: string[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout:
    '{"model":"model-b","rationale":"Selected for the task"}',
  } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.open', () => ({ value: undefined }));
  on('ui.panes', () => ({ value: [] }));
  on('ui.close', () => ({ value: undefined }));
  on('ui.toast', ($, e) => { toasts.push(e.text); return { value: undefined }; });
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

  await $.command.run({ command: 'smart-router', args: 'explain on' });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'unplaced-pane', ...originalInput });
  const shown = await $.ui.mount(row('unplaced-pane'));
  await shown.press({ key: 'why-this-model' });

  expect(toasts).toEqual(['Could not open the native routing explanation pane.']);
  await shown.unmount();
});

test('turning explanations off closes a pane that is still opening', async ($, on) => {
  let finishOpen!: () => void;
  let startedOpen!: () => void;
  const started = new Promise<void>((resolve) => { startedOpen = resolve; });
  const ready = new Promise<void>((resolve) => { finishOpen = resolve; });
  let placed = false;
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout:
    '{"model":"model-b","rationale":"Selected for the task"}',
  } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.open', async () => {
    startedOpen();
    await ready;
    placed = true;
    return { value: undefined };
  });
  on('ui.panes', () => ({ value: [{ id: 'smart-router-explanation', title: 'Why this model?',
    isPlaced: placed, isShown: placed, isFocused: placed }] }));
  on('ui.close', () => { placed = false; return { value: undefined }; });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'pane-opening', ...originalInput });
  const opening = $.command.run({ command: 'smart-router', args: 'explain latest' });
  await started;
  await $.command.run({ command: 'smart-router', args: 'explain off' });
  finishOpen();
  expect((await opening).text).toBe('Could not open the native routing explanation pane.');
  expect(placed).toBe(false);
});

for (const placed of [true, false]) {
  test(`pane placement is read independently of the open return value: ${placed}`, async ($, on) => {
    let closes = 0;
    on('session.id', () => ({ value: 'session-1' }));
    on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout:
      '{"model":"model-b","rationale":"Selected for the task"}',
    } }));
    on('tool.call', () => ({ result: 'ok' }));
    on('ui.open', () => ({ value: undefined }));
    on('ui.panes', () => ({ value: [{ id: 'smart-router-explanation', title: 'Why this model?',
      isPlaced: placed, isShown: placed, isFocused: placed }] }));
    on('ui.close', () => { closes++; return { value: undefined }; });
    await $.tool.call({ tool: 'Agent', tool_use_id: 'pane-placement', ...originalInput });
    const result = await $.command.run({ command: 'smart-router', args: 'explain latest' });
    expect(result.text).toBe(placed ? 'Showing the latest routing explanation.'
      : 'Could not open the native routing explanation pane.');
    expect(closes).toBe(placed ? 0 : 1);
  });
}

test('parent receives the successful child model without rewriting requested inputs or results', async ($, on) => {
  const forwarded: any[] = [];
  const finish = new Map<string, (result: any) => void>();
  let allStarted: () => void = () => {};
  const ready = new Promise<void>(resolve => { allStarted = resolve; });
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', ($, e) => {
    const { tool_use_id } = JSON.parse(e.init.stdin);
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: `selected-${tool_use_id}`, rationale: 'Selected for the task',
    }) } };
  });
  on('agent.spawn', ($, e) => e.tool_use_id === 'denied' ? { deny: 'Permission denied' }
    : { agentId: `child-${e.tool_use_id}`, model: e.tool_use_id === 'unknown' ? '' : `launched-${e.tool_use_id}` });
  on('fs.write', () => ({ value: undefined }));
  on('tool.call', ($, e) => {
    forwarded.push(e);
    return new Promise(resolve => {
      finish.set(e.tool_use_id, resolve);
      if (finish.size === 6) allStarted();
    });
  });
  const inputs = ['background', 'foreground', 'unknown', 'denied', 'error', 'no-child'].map(id => ({
    tool: 'Agent' as const, tool_use_id: id, ...originalInput,
    prompt: 'Use requested-model to compute 1+1', name: 'requested-model',
    subagent_type: 'ug-smart-router:ucode-route-requested-model-hash',
    run_in_background: id !== 'foreground',
  }));
  const waiting = inputs.map(input => $.tool.call(input));
  await ready;
  for (const input of [...inputs].reverse()) {
    const id = input.tool_use_id;
    if (id === 'no-child') { finish.get(id)!({ result: 'unchanged', text: 'unchanged' }); continue; }
    const spawn = await $.agent.spawn({
      tool_use_id: id, prompt: input.prompt, description: input.description,
      subagentType: input.subagent_type, parentModel: 'parent-model',
      provider: { plugin: 'engine', tier: 'core' }, background: input.run_in_background, fork: false,
      name: input.name, permissionMode: 'default', parentAgentId: 'parent', cwd: '/workspace',
    });
    finish.get(id)!(spawn.deny ? { deny: spawn.deny }
      : { result: { agentId: spawn.agentId }, text: 'Original child result',
        context: ['Existing context'], ...(id === 'error' ? { isError: true } : {}) });
  }
  const results = await Promise.all(waiting);
  for (const [index, input] of inputs.entries()) {
    expect(forwarded.find(e => e.tool_use_id === input.tool_use_id)).toEqual(input);
    const result = results[index];
    const id = input.tool_use_id;
    if (id === 'denied') { expect(result).toEqual({ deny: 'Permission denied' }); continue; }
    if (id === 'no-child') { expect(result).toEqual({ result: 'unchanged', text: 'unchanged' }); continue; }
    expect(result.result).toEqual({ agentId: `child-${id}` });
    expect(result.text).toBe('Original child result');
    expect(result.context?.[0]).toBe('Existing context');
    if (id === 'error') { expect(result.context).toEqual(['Existing context']); continue; }
    const note = result.context?.[1] ?? '';
    expect(note).toContain(`"agent_id":"child-${id}"`);
    expect(note).toContain(`"selected_model":"selected-${id}"`);
    expect(note).toContain(id === 'unknown' ? '"launched_model":null' : `"launched_model":"launched-${id}"`);
    expect(note).toContain('Report only that model in ordinary updates and answers');
    expect(note).toContain('unless the user asks about the difference');
    expect(note).not.toContain('requested-model');
    expect(note).not.toContain('1+1');
  }
});

async function collectParentTurn($: any, turnId: string, index = 1) {
  const stream = $.turn.step({
    turnId, index, model: 'parent-model', messageCount: 3, agentId: undefined,
  });
  const chunks: any[] = [];
  let item = await stream.next();
  while (!item.done) {
    chunks.push(item.value);
    item = await stream.next();
  }
  return {
    chunks,
    result: item.value,
    text: chunks.filter((chunk) => chunk.kind === 'text').map((chunk) => chunk.text).join(''),
  };
}

test('routed children leave native parent synthesis and stream errors unchanged', async ($, on) => {
  on('env.get', ($, e) => ({value: e.name === 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY' ? '1' : undefined}));
  const selected: Record<string, string> = { 'call-a': 'claude-sonnet-5', 'call-b': 'claude-opus-4-8' };
  const launched: Record<string, string> = { 'call-a': 'system.ai.claude-sonnet-5', 'call-b': 'claude-opus-4-8' };
  const inputs = ['call-a', 'call-b'].map((id) => ({
    tool: 'Agent' as const, tool_use_id: id, prompt: 'Inspect ' + id,
    description: 'Inspect ' + id, subagent_type: 'Explore', model: 'sonnet',
    run_in_background: true,
  }));
  const forwardedSpawns: any[] = [];
  const parentAnswer = 'Child A found two files; Child B found three files.';
  const thinking = { kind: 'thinking' as const, index: 0, text: 'I compared both findings.' };
  const firstText = { kind: 'text' as const, index: 1, text: 'Child A found two files; ' };
  const secondText = { kind: 'text' as const, index: 2, text: 'Child B found three files.' };
  const stop = { kind: 'stop' as const, stopReason: 'end_turn', usage: null };
  const nativeError = new Error('synthetic native stream failure');
  const errorThinking = { kind: 'thinking' as const, index: 0, text: 'Reasoning before failure.' };
  const errorText = { kind: 'text' as const, index: 1, text: 'Partial native answer.' };
  let childStepModel = '';

  on('session.id', () => ({ value: 'session-1' }));
  on('clock.every', () => ({ value: { cancel: () => {} } }));
  on('process.run', ($, event) => {
    const id = JSON.parse(event.init.stdin).tool_use_id;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: selected[id], rationale: 'Selected for the task',
    }) } };
  });
  on('fs.write', () => ({ value: undefined }));
  on('tool.call', () => ({ result: 'Child launched', text: 'Child launched' }));
  on('agent.spawn', ($, event) => {
    forwardedSpawns.push(event);
    return { agentId: 'child-' + event.tool_use_id, model: launched[event.tool_use_id] };
  });
  on('turn.complete', () => ({ text: '' }));
  on('turn.step', async function* ($, event) {
    if (event.turnId === 'parent-synthesis') {
      yield thinking;
      yield firstText;
      yield secondText;
      yield stop;
      return {
        turnId: event.turnId, index: event.index, answer: parentAnswer,
        toolUses: [], stopReason: 'end_turn', usage: null,
      };
    }
    childStepModel = event.model;
    yield errorThinking;
    yield errorText;
    throw nativeError;
  });

  for (const input of inputs) {
    await $.tool.call(input);
    const spawn = {
      tool_use_id: input.tool_use_id, prompt: input.prompt, description: input.description,
      subagentType: input.subagent_type, parentModel: 'parent-model',
      provider: { plugin: 'engine', tier: 'core' as const }, background: true, fork: false,
      name: 'caller-' + input.tool_use_id, permissionMode: 'default',
      parentAgentId: undefined, cwd: '/workspace',
    };
    const child = await $.agent.spawn(spawn);
    expect(child).toEqual({ agentId: 'child-' + input.tool_use_id, model: launched[input.tool_use_id] });
    expect(forwardedSpawns[forwardedSpawns.length - 1]).toEqual({
      ...spawn, model: selected[input.tool_use_id],
    });
    await $.turn.complete({
      agentId: child.agentId, turnId: 'child-turn-' + input.tool_use_id,
      answer: 'Answer from ' + input.tool_use_id, reason: 'answer',
      isAborted: false, durationMs: 1,
    });
  }

  const parent = await collectParentTurn($, 'parent-synthesis', 1);
  expect(parent.chunks).toEqual([thinking, firstText, secondText, stop]);
  expect(parent.text).toBe(parentAnswer);
  expect(parent.result).toEqual({
    turnId: 'parent-synthesis', index: 1, answer: parentAnswer,
    toolUses: [], stopReason: 'end_turn', usage: null,
  });

  const stream = $.turn.step({
    turnId: 'child-stream-error', index: 1, model: 'parent-model',
    messageCount: 3, agentId: 'child-call-a',
  });
  const chunks: any[] = [];
  let caught: unknown;
  try {
    let item = await stream.next();
    while (!item.done) {
      chunks.push(item.value);
      item = await stream.next();
    }
  } catch (error) {
    caught = error;
  }
  expect(childStepModel).toBe(launched['call-a']);
  expect(chunks).toEqual([errorThinking, errorText]);
  let controlError: unknown;
  try { await collectParentTurn($, 'unrouted-stream-error', 1); }
  catch (error) { controlError = error; }
  expect(String(caught)).toContain('no implementation for turn.step');
  expect(String(caught)).toBe(String(controlError));
});

test('a spawn step preserves native text and thinking with reviewed tool inputs', async ($, on) => {
  on('env.get', ($, e) => ({value: e.name === 'UCODE_SESSION_ENV_FILE' ? '/controls.json' : '0'}));
  on('fs.read', () => ({value: JSON.stringify({ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1'})}));
  const claim = 'I am using the requested model.';
  const tools = [{name: 'Agent', input: originalInput}, {name: 'Read', input: {file_path: 'README.md'}}];
  on('turn.step', async function* ($, e) {
    yield {kind: 'thinking', index: 0, text: claim};
    yield {kind: 'text', index: 1, text: claim};
    yield {kind: 'tool', index: 2, id: 'spawn', name: 'Agent'};
    yield {kind: 'input', index: 2, json: JSON.stringify(originalInput)};
    yield {kind: 'tool', index: 3, id: 'read', name: 'Read'};
    yield {kind: 'input', index: 3, json: JSON.stringify(tools[1].input)};
    return {turnId: e.turnId, index: e.index, answer: claim, toolUses: tools, stopReason: 'tool_use', usage: null};
  });
  const spawn = await collectParentTurn($, 'before-spawn', 0);
  expect(spawn.text).toBe(claim);
  expect(spawn.result.answer).toBe(claim);
  expect(spawn.result.toolUses).toEqual(tools);
  expect(spawn.chunks.map(chunk => chunk.kind)).toEqual(['thinking', 'text', 'tool', 'input', 'tool', 'input']);
  expect(spawn.chunks[3].json).toBe(JSON.stringify(originalInput));
});
