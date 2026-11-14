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

test('child turns preserve native context suffixes without changing exact catalog identities', async ($, on) => {
  const cases = [
    ['main.default.claude-sonnet-4.5', 'main.default.claude-sonnet-4.5[1m]', 'main.default.claude-sonnet-4.5[1m]'],
    ['system.ai.model-b', 'anthropic-aigw-12345678-system.ai.model-b[1m]', 'anthropic-aigw-12345678-system.ai.model-b[1m]'],
    ['system.ai.model-b', 'anthropic-aigw-c442a48-system.ai.model-b[1m]', 'anthropic-aigw-c442a48-system.ai.model-b[1m]'],
    ['custom.catalog.code', 'custom.catalog.code[1m]', 'custom.catalog.code'],
    ['model-b', 'model-c', 'model-b'],
  ];
  let selected = '';
  let launched = '';
  let stepModel = '';
  const forwarded: any[] = [];
  on('env.get', () => ({ value: '0' }));
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
    model: selected, rationale: 'Selected for the task',
  }) } }));
  on('tool.call', ($, e) => { forwarded.push(e); return { result: 'ok' }; });
  on('agent.spawn', ($, e) => {
    expect(e.model).toBe(selected);
    return { agentId: `child-${e.tool_use_id}`, model: launched };
  });
  on('fs.write', () => ({ value: undefined }));
  on('turn.step', async function* ($, e) {
    stepModel = e.model;
    yield { kind: 'text', index: 0, text: 'Native child response' };
    return { turnId: e.turnId, index: e.index, answer: 'Native child response', toolUses: [], stopReason: 'end_turn', usage: null };
  });
  for (const [index, [route, resolved, expected]] of cases.entries()) {
    selected = route;
    launched = resolved;
    const id = `context-${index}`;
    const input = { tool: 'Agent' as const, tool_use_id: id, ...originalInput };
    await $.tool.call(input);
    expect(forwarded[index]).toEqual(input);
    await $.agent.spawn({ tool_use_id: id, prompt: originalInput.prompt });
    const stream = $.turn.step({ turnId: id, index: 1, model: 'parent-model', messageCount: 1, agentId: `child-${id}` });
    expect((await stream.next()).value).toEqual({ kind: 'text', index: 0, text: 'Native child response' });
    expect((await stream.next()).done).toBe(true);
    expect(stepModel).toBe(expected);
  }
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

test('parallel cards open each call’s own routing explanation', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
  const releases = new Map<string, () => void>();
  let started: () => void = () => {};
  const bothStarted = new Promise<void>((resolve) => { started = resolve; });
  on('session.id', () => ({ value: 'session-1' }));
  on('clock.every', () => ({ value: { cancel: () => {} } }));
  on('process.run', async ($, e) => {
    const request = JSON.parse(e.init.stdin);
    const { tool_use_id } = request;
    await new Promise<void>((resolve) => {
      releases.set(tool_use_id, resolve);
      if (releases.size === 2) started();
    });
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.model-b',
      rationale: `Selected for ${request.tool_input.description}`,
      explanation: { ...explanation, summary: `Explanation for ${tool_use_id}`,
        session_id: request.session_id, tool_use_id },
      session_id: request.session_id,
      tool_use_id,
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  let panePlaced = false;
  on('ui.open', () => { panePlaced = true; return { value: { isPlaced: true } }; });

  on('ui.close', () => { panePlaced = false; return { value: undefined }; });
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['other row'] }));
  await $.command.run({ command: 'smart-router', args: 'explain on' });

  const input = { ...originalInput, run_in_background: true };
  const callA = $.tool.call({ tool: 'Agent', tool_use_id: 'call-a', ...input, description: 'Compute 1+1 (agent A)' });
  const callB = $.tool.call({ tool: 'Task', tool_use_id: 'call-b', ...input, description: 'Compute 1+1 (agent B)' });
  await bothStarted;

  releases.get('call-b')!();
  await callB;
  releases.get('call-a')!();
  await callA;
  const cards = await $.ui.mount(reply('reply-after-both-routes'));
  const tree = JSON.stringify(await cards.drawn());
  expect(tree).toContain('Compute 1+1 (agent A)');
  expect(tree).toContain('Compute 1+1 (agent B)');
  const whyButtons = await cards.findAll({ type: 'Button', text: 'Why this model?' });
  expect(whyButtons.length).toBe(2);
  expect(new Set(whyButtons.map(({ key }) => key)).size).toBe(2);

  for (const [id, key] of [['call-b', 'why-this-model-call-b'], ['call-a', 'why-this-model-call-a']]) {
    await cards.press({ key });
    const pane = await $.ui.mount({
      ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation',
    });
    expect(JSON.stringify(await pane.drawn())).toContain(`Explanation for ${id}`);
    await pane.press({ key: 'close' });
    await pane.unmount();
  }
  await cards.unmount();
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

for (const [modelCount, customPrefix, routedModelOverride] of [
  [2, undefined, undefined],
  [20, undefined, undefined],
  [2, 'custom.catalog', undefined],
  [2, 'custom.catalog.withdots', undefined],
  [2, undefined, 'anthropic-aigw-c442a48-system.ai.model-b'],
] as const) {
const label = routedModelOverride ? 'explanations with a seven-digit gateway wrapper' :
  `explanations for ${modelCount} models with custom prefix ${customPrefix ?? 'none'}`
test(`${label} preserve the route`, { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
  const canonicalJourney = modelCount === 2 && !customPrefix && !routedModelOverride;
  const customModel = customPrefix ? `${customPrefix}.code` : undefined;
  const details = customModel ? {
    ...explanation, selected_model: customModel,
    models: explanation.models.map((model) => ({ ...model,
      id: model.selected ? customModel : `${customModel}[1m]`,
    })),
  } : explanation;
  const routedModel = routedModelOverride ?? (customModel
    ? `anthropic-aigw-01234567-${customModel}`
    : modelCount === 20 ? 'anthropic-aigw-01234567-system.ai.model-b[1m]' : explanationModel);
  let routeCalls = 0;
  let closes = 0;
  const spawns: any[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', ($, e) => {
    routeCalls++;
    const request = JSON.parse(e.init.stdin);
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: routedModel, rationale: 'Selected for the task', explanation: {
        ...details, session_id: request.session_id, tool_use_id: request.tool_use_id,
        ...(modelCount === 20 ? { formula: 'quality - cost' } : {}),
        models: [...details.models, ...Array.from({ length: modelCount - 2 }, (_, i) => ({
          id: `alternative-${i}`, name: `Alternative ${i}`, selected: false, considered: i !== 16,
          reason: i === 16 ? 'Unavailable for this request.' : 'Lower expected fit.',
        }))],
      }, session_id: request.session_id, tool_use_id: request.tool_use_id,
    }) } };
  });
  on('tool.call', () => ({ result: 'ok' }));
  on('agent.spawn', ($, e) => { spawns.push(e); return { model: e.model, agentId: `child-${e.tool_use_id}` }; });
  on('fs.write', () => ({ value: undefined }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));
  on('ui.open', () => ({ value: { isPlaced: true } }));

  on('ui.close', () => { closes++; return { value: undefined }; });

  if (canonicalJourney) {
    await $.tool.call({ tool: 'Agent', tool_use_id: 'off-by-default', ...originalInput });
    const hidden = await $.ui.mount(row('off-by-default'));
    expect(JSON.stringify(await hidden.drawn())).not.toContain('Why this model?');
    await hidden.unmount();
  }

  await $.command.run({ command: 'smart-router', args: 'explain on' });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'shown', ...originalInput });
  const shown = await $.ui.mount(row('shown'));
  expect(JSON.stringify(await shown.drawn())).toContain('Why this model?');
  await shown.press({ key: 'why-this-model-shown' });
  await shown.unmount();

  if (canonicalJourney) {
    expect((await $.command.run({ command: 'smart-router', args: 'explain latest' })).text)
      .toBe('Showing the latest routing explanation.');
    expect(closes).toBe(0);
  }
  const pane = await $.ui.mount({ ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation' });
  if (modelCount === 20) {
    const routeCallsBeforeCompare = routeCalls;
    await pane.select({ key: 'compare-model', value: 'alternative-17' });
    const lastAlternative = JSON.stringify(await pane.drawn());
    expect(lastAlternative).toContain('Alternative 17 — considered');
    expect(lastAlternative).toContain('Lower expected fit.');
    expect(lastAlternative).toContain('Show numbers');
    expect(lastAlternative).not.toContain('Scoring formula:');
    await pane.press({ key: 'numbers' });
    const formulaOnly = JSON.stringify(await pane.drawn());
    expect(formulaOnly).toContain('Scoring formula: quality - cost');
    expect(formulaOnly).not.toContain('Est. cost:');
    await pane.select({ key: 'compare-model', value: 'alternative-16' });
    const excluded = JSON.stringify(await pane.drawn());
    expect(excluded).toContain('Alternative 16 — not considered');
    expect(excluded).toContain('Unavailable for this request.');
    expect(routeCalls).toBe(routeCallsBeforeCompare);
  } else if (canonicalJourney) {
    const tree = JSON.stringify(await pane.drawn());
    for (const text of ['best expected quality', 'Model A', 'Model B', 'Chosen for its expected quality', 'Show numbers']) {
      expect(tree).toContain(text);
    }
    for (const text of ['hidden-request', 'hidden-source', 'session-1', 'shown']) {
      expect(tree).not.toContain(text);
    }
    expect(tree).not.toContain('Est. cost');
    await pane.select({ key: 'compare-model', value: 'model-a' });
    expect(JSON.stringify(await pane.drawn())).toContain('The router expected a weaker fit');
    await pane.press({ key: 'numbers' });
    const numbers = ['Quality estimate: 0.800', 'Est. cost: 0.0004 USD',
      'Routing score: 0.700', 'Components: quality 0.800', 'Vs. routed: quality -0.100'];
    const visibleNumbers = JSON.stringify(await pane.drawn());
    for (const text of numbers) expect(visibleNumbers).toContain(text);
    await pane.press({ key: 'numbers' });
    const hiddenNumbers = JSON.stringify(await pane.drawn());
    for (const text of numbers) expect(hiddenNumbers).not.toContain(text);
    expect(hiddenNumbers).toContain('Show numbers');
  } else {
    expect(JSON.stringify(await pane.drawn())).toContain('Chosen: Model B');
    await pane.select({ key: 'compare-model', value: customModel ? `${customModel}[1m]` : 'model-a' });
    const comparison = JSON.stringify(await pane.drawn());
    expect(comparison).toContain('Model A — considered');
    expect(comparison).toContain('The router expected a weaker fit');
  }
  await pane.unmount();

  await $.agent.spawn({ tool_use_id: 'shown', name: 'caller', prompt: 'inspect' });
  expect(spawns[0].model).toBe(routedModel);
  expect(routeCalls).toBe(canonicalJourney ? 2 : 1);
  if (canonicalJourney) {
    await $.command.run({ command: 'smart-router', args: 'explain off' });
    const off = await $.ui.mount(row('shown'));
    expect(JSON.stringify(await off.drawn())).not.toContain('Why this model?');
    await off.unmount();
  }
});

}

test('unknown explanation actions do not open the latest routing explanation', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
  let opens = 0;
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
    model: 'system.ai.model-b', rationale: 'Selected for the task',
  }) } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.open', () => { opens++; return { value: { isPlaced: true } }; });

  await $.tool.call({ tool: 'Agent', tool_use_id: 'latest', ...originalInput });
  await $.command.run({ command: 'smart-router', args: 'explain unexpected' });
  expect(opens).toBe(0);
});

test('explain on reports unavailable when subagent routing is not configured', { options: {
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', routeCommand: [],
} }, async ($, on) => {
  let routeCalls = 0;
  on('process.run', () => { routeCalls++; return { value: { exitCode: 0, stderr: '', stdout: '' } }; });
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

  expect((await $.command.run({ command: 'smart-router', args: 'explain on' })).text)
    .toBe('Routing explanations are unavailable because subagent routing is not configured.');
  await $.tool.call({ tool: 'Agent', tool_use_id: 'unrouted', ...originalInput });
  const shown = await $.ui.mount(row('unrouted'));
  expect(JSON.stringify(await shown.drawn())).not.toContain('Why this model?');
  expect(routeCalls).toBe(0);
  await shown.unmount();
});

test('explanation costs use decimal notation without rounding small costs to zero', { options: {
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
} }, async ($, on) => {
  let cost = 0;
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
    model: explanationModel, rationale: 'Selected for the task', explanation: { ...explanation,
      models: explanation.models.map((model) => ({ ...model, estimated_cost_usd: cost })),
    },
  }) } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));
  on('ui.open', () => ({ value: { isPlaced: true } }));


  for (const [index, [value, expected]] of ([
    [0, '0'], [1234.567, '1234.567'], [0.000001, '0.000001'],
    [0.00000012345, '<0.000001'], [Number.MIN_VALUE, '<0.000001'],
  ] as const).entries()) {
    cost = value;
    await $.tool.call({ tool: 'Agent', tool_use_id: `cost-${index}`, ...originalInput });
    await $.command.run({ command: 'smart-router', args: 'explain latest' });
    const pane = await $.ui.mount({ ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation' });
    await pane.press({ key: 'numbers' });
    expect(JSON.stringify(await pane.drawn())).toContain(`Est. cost: ${expected} USD`);
    await pane.unmount();
  }
});

for (const [selectedModel, routedModel, explanationId, accepted] of [
  ['gpt-5.4.1', 'gpt-5.4.1[1m]', 'gpt-5.4.1', true],
  ['databricks-gpt-5.4.1', 'databricks-gpt-5.4.1[1m]', 'databricks-gpt-5.4.1', true],
  ['anthropic-aigw-01234567-gpt-5.4.1', 'anthropic-aigw-01234567-gpt-5.4.1[1m]', 'gpt-5.4.1', true],
  ['gpt-5.4.1[1m]', 'gpt-5.4.1[1m]', 'gpt-5.4.1[1m]', true],
  ['custom.catalog.gpt-5.4.1', 'custom.catalog.gpt-5.4.1', 'custom.catalog.gpt-5.4.1', true],
  ['custom.catalog.gpt-5.4.1[1m]', 'custom.catalog.gpt-5.4.1[1m]', 'custom.catalog.gpt-5.4.1[1m]', true],
  ['custom.catalog.gpt-5.4.1[1m]', 'custom.catalog.gpt-5.4.1[1m]', 'custom.catalog.gpt-5.4.1', false],
  ['custom.catalog.gpt-5.4.1', 'custom.catalog.gpt-5.4.1[1m]', 'custom.catalog.gpt-5.4.1', false],
  ['gpt-5.4.1', 'gpt-5.4.2[1m]', 'gpt-5.4.1', false],
] as const) {
  test(`explanation handoff ${selectedModel} to ${routedModel} with ${explanationId}`, { options: {
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  } }, async ($, on) => {
    const spawns: string[] = [];
    on('session.id', () => ({ value: 'session-1' }));
    on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: routedModel, selected_model: selectedModel, rationale: 'Original route reason',
      explanation: { ...explanation, selected_model: explanationId,
        models: explanation.models.map((model) => ({ ...model, id: model.selected ? explanationId : 'alternative' })),
      },
    }) } }));
    on('tool.call', () => ({ result: 'ok' }));
    on('agent.spawn', ($, e) => { spawns.push(e.model!); return { model: e.model }; });
    on('ui.open', () => ({ value: { isPlaced: true } }));
    await $.tool.call({ tool: 'Agent', tool_use_id: 'handoff', ...originalInput });
    await $.command.run({ command: 'smart-router', args: 'explain latest' });
    const pane = await $.ui.mount({ ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation' });
    const tree = JSON.stringify(await pane.drawn());
    if (accepted) {
      expect(tree).toContain('Chosen: Model B');
      expect(tree).toContain('Chosen for its expected quality.');
    } else {
      expect(tree).toContain('No routing explanation details were returned.');
      expect(tree).toContain('Original route reason');
    }
    await pane.unmount();
    await $.agent.spawn({ tool_use_id: 'handoff', prompt: 'inspect' });
    expect(spawns).toEqual([routedModel]);
  });
}

for (const [name, details, diagnostic] of [
  ['missing', undefined, false],
  ['malformed', { ...explanation, selected_model: 'model-c' }, true],
  ['corrupted selected identity', { ...explanation, selected_model: '\u001b[31mmodel-b\u001b[0m' }, true],
  ['corrupted candidate identity', { ...explanation, models: explanation.models.map((model) => ({
    ...model, id: `\u001b[31m${model.id}\u001b[0m`,
  })) }, true],
  ['trailing newline in candidate identity', { ...explanation, models: explanation.models.map((model) => ({
    ...model, id: model.selected ? model.id : `${model.id}\n`,
  })) }, true],
  ['wrong session identity', { ...explanation, session_id: 'another-session' }, true],
  ['wrong tool identity', { ...explanation, tool_use_id: 'another-tool' }, true],
  ['nested session conflict', { ...explanation, session_id: 'session-1', tool_use_id: 'nested session conflict',
    evidence: { session_id: 'another-session' } }, true],
  ['nested tool conflict', { ...explanation, session_id: 'session-1', tool_use_id: 'nested tool conflict',
    evidence: { tool_use_id: 'another-tool' } }, true],
] as const) {
  test(`${name} explanation shows no details while preserving the route`, { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
    const logs: string[] = [];
    const logDestinations: unknown[] = [];
    const routed = 'system.ai.model-b[1m]';
    const forwarded: any[] = [];
    on('session.id', () => ({ value: 'session-1' }));
    on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: routed, rationale: 'Original route reason', ...(details ? { explanation: details } : {}),
    }) } }));
    on('tool.call', () => ({ result: 'ok' }));
    on('agent.spawn', ($, e) => { forwarded.push(e); return { model: e.model }; });
    on('ui.log', ($, e) => { logs.push(e.text); logDestinations.push(e.to); return { value: undefined }; });
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));
    on('ui.open', () => ({ value: { isPlaced: true } }));

    on('ui.close', () => ({ value: undefined }));

    await $.tool.call({ tool: 'Agent', tool_use_id: name, ...originalInput });
    expect(logs).toEqual(diagnostic
      ? ['Smart Router returned invalid explanation details; keeping its selected model.'] : []);
    expect(logDestinations).toEqual(diagnostic ? ['debug'] : []);
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
  test(`a mismatched route-level ${mismatchedField} preserves the selected model`, { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
    const logs: string[] = [];
    const spawns: any[] = [];
    on('session.id', () => ({ value: 'session-1' }));
    on('process.run', ($, e) => {
      const request = JSON.parse(e.init.stdin);
      return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
        model: 'system.ai.model-b',
        rationale: 'Selected for the task',
        explanation: { ...explanation, session_id: request.session_id, tool_use_id: request.tool_use_id },
        [mismatchedField]: 'another-request',
      }) } };
    });
    on('tool.call', () => ({ result: 'ok' }));
    on('agent.spawn', ($, e) => { spawns.push(e); return { model: e.model ?? 'native-default' }; });
    on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
    on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));
    on('ui.open', () => ({ value: { isPlaced: true } }));

    on('ui.close', () => ({ value: undefined }));

    await $.tool.call({ tool: 'Agent', tool_use_id: 'request-1', ...originalInput });
    await $.command.run({ command: 'smart-router', args: 'explain latest' });
    const pane = await $.ui.mount({ ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation' });
    const tree = JSON.stringify(await pane.drawn());
    expect(tree).toContain('No routing explanation details were returned.');
    expect(tree).not.toContain('Model A');
    await pane.unmount();
    await $.agent.spawn({ tool_use_id: 'request-1', prompt: 'inspect' });

    expect(spawns[0].model).toBe('system.ai.model-b');
    expect(logs).toEqual(['Smart Router returned invalid explanation details; keeping its selected model.']);
  });
}

for (const reason of ['clear', 'resume'] as const) {
test(`session ${reason} during native ToolUse rendering restores the original tool label`, { options: {
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
} }, async ($, on) => {
  let renderCalls = 0;
  const renderedTools: string[] = [];
  let renderStarted!: () => void;
  let finishRender!: () => void;
  const started = new Promise<void>((resolve) => { renderStarted = resolve; });
  const ready = new Promise<void>((resolve) => { finishRender = resolve; });
  on('session.id', () => ({ value: 'session-1' }));
  on('session.end', () => ({ sessionId: 'session-1' }));
  on('clock.every', () => ({ value: { cancel: () => {} } }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
    model: explanationModel, rationale: 'Selected for the task', explanation,
  }) } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.close', () => ({ value: undefined }));
  on('ui.render', async ($, e) => {
    renderCalls++;
    renderedTools.push(e.props.tool);
    if (renderCalls === 1) { renderStarted(); await ready; }
    return { type: 'Text', props: {}, children: [`original ${e.props.tool} row`] };
  });

  await $.command.run({ command: 'smart-router', args: 'explain on' });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'rendering', ...originalInput });
  const rendering = $.ui.render(row('rendering'));
  await started;
  await $.session.end({ reason });
  finishRender();
  const rendered = await rendering;
  expect(renderCalls).toBe(2);
  expect(renderedTools[0]).not.toBe('Agent');
  expect(renderedTools[1]).toBe('Agent');
  const tree = JSON.stringify(rendered);
  expect(tree).toContain('original Agent row');
  expect(tree).not.toContain('Selected Model');
  expect(tree).not.toContain('Why this model?');
  expect(tree).not.toContain('Selecting a model');
});

test(`session ${reason} clears routing UI while pending routes keep their native spawn and receipt`, { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
  on('session.end', () => ({ sessionId: 'session-1' }));
  let closes = 0;
  let opens = 0;
  let panePlaced = false;
  let completePendingRoute: (() => void) | undefined;
  let onRouteStarted: (() => void) | undefined;
  const routeStarted = new Promise<void>((resolve) => { onRouteStarted = resolve; });
  let completePendingSpawn: (() => void) | undefined;
  let onSpawnStarted: (() => void) | undefined;
  const spawnStarted = new Promise<void>((resolve) => { onSpawnStarted = resolve; });
  const finish = new Map<string, (result: any) => void>();
  let onRouteForwarded: () => void = () => {};
  const routeForwarded = new Promise<void>((resolve) => { onRouteForwarded = resolve; });
  let onSpawnForwarded: () => void = () => {};
  const spawnForwarded = new Promise<void>((resolve) => { onSpawnForwarded = resolve; });
  const stepModels: string[] = [];
  on('env.get', ($, e) => ({ value: e.name === 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY' ? '1' : undefined }));
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', ($, e) => {
    const request = JSON.parse(e.init.stdin);
    const response = { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.model-b',
      rationale: 'Selected for the task',
      explanation: { ...explanation, session_id: request.session_id, tool_use_id: request.tool_use_id },
      session_id: request.session_id,
      tool_use_id: request.tool_use_id,
    }) } };
    if (request.tool_use_id !== 'pending-before-clear') return response;
    return new Promise((resolve) => {
      completePendingRoute = () => resolve(response);
      onRouteStarted?.();
    });
  });
  const forwarded: any[] = [];
  on('tool.call', ($, e) => {
    forwarded.push(e);
    if (e.tool_use_id.startsWith('pending-')) {
      return new Promise((resolve) => {
        finish.set(e.tool_use_id, resolve);
        if (e.tool_use_id === 'pending-before-clear') onRouteForwarded();
        else onSpawnForwarded();
      });
    }
    return { result: 'ok' };
  });
  const spawns: any[] = [];
  on('agent.spawn', async ($, e) => {
    spawns.push(e);
    if (e.tool_use_id === 'pending-spawn') {
      await new Promise<void>((resolve) => { completePendingSpawn = resolve; onSpawnStarted?.(); });
    }
    return { model: e.model, agentId: `child-${e.tool_use_id}` };
  });
  const writes: any[] = [];
  on('fs.write', ($, e) => { writes.push(e); return { value: undefined }; });
  on('ui.render', ($, e) => ({ type: 'Text', props: {}, children: [e.props.tool ?? 'original reply'] }));
  on('ui.open', () => { opens++; panePlaced = true; return { value: { isPlaced: true } }; });

  on('ui.close', () => { closes++; panePlaced = false; return { value: undefined }; });
  on('turn.step', async function* ($, e) {
    stepModels.push(e.model);
    yield { kind: 'text', index: 0, text: 'Native child response' };
    return { turnId: e.turnId, index: e.index, answer: 'Native child response', toolUses: [], stopReason: 'end_turn', usage: null };
  });

  await $.command.run({ command: 'smart-router', args: 'explain on' });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'before-clear', ...originalInput });
  const oldWhy = await $.ui.mount(row('before-clear'));
  expect(await oldWhy.find({ type: 'Button', key: 'why-this-model-before-clear' })).toBeDefined();
  await $.ui.press({ plugin: 'ug-smart-router', key: 'why-this-model-before-clear', surface: 'terminal' });
  expect(opens).toBe(1);
  expect((await $.command.run({ command: 'smart-router', args: 'explain latest' })).text)
    .toBe('Showing the latest routing explanation.');
  expect(panePlaced).toBe(true);
  const pendingRoute = $.tool.call({ tool: 'Agent', tool_use_id: 'pending-before-clear', ...originalInput });
  await routeStarted;
  await $.tool.call({ tool: 'Agent', tool_use_id: 'anchored-before-clear', ...originalInput });
  const anchored = await $.ui.mount(reply('anchored'));
  expect(JSON.stringify(await anchored.drawn())).toContain('Selected Model');
  await anchored.unmount();
  const pendingSpawn = $.tool.call({ tool: 'Agent', tool_use_id: 'pending-spawn', ...originalInput });
  await spawnForwarded;
  const nativeSpawn = $.agent.spawn({ tool_use_id: 'pending-spawn', prompt: originalInput.prompt });
  await spawnStarted;
  const closesBeforeEnd = closes;
  const opensBeforeEnd = opens;
  await $.session.end({ reason });

  expect(closes).toBe(closesBeforeEnd + 1);
  expect(panePlaced).toBe(false);
  // Direct press dispatches the still-hosted button; mounted.press would redraw
  // the invalidated row first and remove the stale callback we want to exercise.
  await $.ui.press({ plugin: 'ug-smart-router', key: 'why-this-model-before-clear', surface: 'terminal' });
  expect(opens).toBe(opensBeforeEnd);
  expect(panePlaced).toBe(false);
  await oldWhy.unmount();
  expect((await $.command.run({ command: 'smart-router', args: 'explain latest' })).text)
    .toBe('No completed routing decision is available in this session.');
  completePendingRoute?.();
  await routeForwarded;
  const routedChild = await $.agent.spawn({ tool_use_id: 'pending-before-clear', prompt: originalInput.prompt });
  finish.get('pending-before-clear')!({ result: routedChild.agentId, context: ['Native context'] });
  completePendingSpawn?.();
  const spawnedChild = await nativeSpawn;
  finish.get('pending-spawn')!({ result: spawnedChild.agentId, context: ['Native context'] });
  const results = await Promise.all([pendingRoute, pendingSpawn]);
  for (const [index, id] of ['pending-before-clear', 'pending-spawn'].entries()) {
    expect(forwarded.find(e => e.tool_use_id === id)).toEqual({ tool: 'Agent', tool_use_id: id, ...originalInput });
    expect(spawns.find(e => e.tool_use_id === id).model).toBe('system.ai.model-b');
    expect(results[index].result).toBe(`child-${id}`);
    expect(results[index].context?.[0]).toBe('Native context');
    expect(results[index].context?.[1]).toContain(`"agent_id":"child-${id}"`);
    expect(results[index].context?.[1]).toContain('"selected_model":"system.ai.model-b"');
    expect(results[index].context?.[1]).toContain('"launched_model":"system.ai.model-b"');
    const stream = $.turn.step({ turnId: id, index: 1, model: 'parent-model', messageCount: 1, agentId: `child-${id}` });
    expect((await stream.next()).value).toEqual({ kind: 'text', index: 0, text: 'Native child response' });
    expect((await stream.next()).done).toBe(true);
    expect(stepModels[index]).toBe('system.ai.model-b');
  }
  expect(writes).toEqual([]);
  expect((await $.command.run({ command: 'smart-router', args: 'explain latest' })).text)
    .toBe('No completed routing decision is available in this session.');
  for (const id of ['before-clear', 'anchored-before-clear', 'pending-before-clear', 'pending-spawn']) {
    const oldRow = await $.ui.mount(row(id));
    expect(await oldRow.find({ type: 'Text', text: 'Agent' })).toBeDefined();
    expect(await oldRow.find({ type: 'Text', text: /Selected Model|Selecting a model|Why this model/ })).toBeUndefined();
    await oldRow.unmount();
  }
  for (const id of ['anchored', 'new-reply']) {
    const oldReply = await $.ui.mount(reply(id));
    expect(await oldReply.find({ type: 'Text', text: /Selected Model|Why this model/ })).toBeUndefined();
    await oldReply.unmount();
  }
  // Reusing a formerly grouped row must behave as a fresh lone call.
  await $.tool.call({ tool: 'Agent', tool_use_id: 'anchored-before-clear', ...originalInput });
  const afterClear = await $.ui.mount(row('anchored-before-clear'));
  expect(JSON.stringify(await afterClear.drawn())).toContain('Why this model?');
  await afterClear.unmount();
});
}


test('explanation parsing failures preserve a valid routed spawn', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
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


for (const failure of ['unplaced', 'open', 'close']) {
test(`the Why button reports ${failure} failure and clears its pane record`, { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
  const toasts: string[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout:
    '{"model":"model-b","rationale":"Selected for the task"}',
  } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.open', () => {
    if (failure === 'open') throw new Error('Pane opening failed');
    return { value: { isPlaced: false, reason: 'no surface' } };
  });
  on('ui.close', () => {
    if (failure === 'close') throw new Error('Pane closing failed');
    return { value: undefined };
  });
  on('ui.toast', ($, e) => { toasts.push(e.text); return { value: undefined }; });
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));

  await $.command.run({ command: 'smart-router', args: 'explain on' });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'unplaced-pane', ...originalInput });
  const shown = await $.ui.mount(row('unplaced-pane'));
  await shown.press({ key: 'why-this-model-unplaced-pane' });

  expect(toasts).toEqual(['Could not open the native routing explanation pane.']);
  const stalePane = await $.ui.mount({ ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation' });
  const rendered = JSON.stringify(await stalePane.drawn());
  expect(rendered).toContain('original Agent row');
  expect(rendered).not.toContain('Selected route:');
  await stalePane.unmount();
  await shown.unmount();
});
}

for (const outcome of ['failure', 'success'] as const) {
for (const cancellation of ['none', 'off', 'clear', 'resume'] as const) {
test(`a late pane-opening ${outcome} respects a newer open followed by ${cancellation}`, { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
  let finishFirst!: () => void;
  let startedFirst!: () => void;
  const started = new Promise<void>((resolve) => { startedFirst = resolve; });
  const ready = new Promise<void>((resolve) => { finishFirst = resolve; });
  let calls = 0;
  let closes = 0;
  let placed = false;
  on('session.id', () => ({ value: 'session-1' }));
  on('session.end', () => ({ sessionId: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout:
    '{"model":"model-b","rationale":"Selected for the task"}',
  } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.open', async () => {
    if (calls++ === 0) {
      startedFirst();
      await ready;
      if (outcome === 'failure') throw new Error('Earlier opening failed');
    }
    placed = true;
    return { value: { isPlaced: true } };
  });
  on('ui.close', () => { closes++; placed = false; return { value: undefined }; });
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['original Agent row'] }));
  await $.tool.call({ tool: 'Agent', tool_use_id: 'overlapping-open', ...originalInput });
  const first = $.command.run({ command: 'smart-router', args: 'explain latest' });
  await started;
  expect((await $.command.run({ command: 'smart-router', args: 'explain latest' })).text)
    .toBe('Showing the latest routing explanation.');
  if (cancellation === 'off') await $.command.run({ command: 'smart-router', args: 'explain off' });
  else if (cancellation !== 'none') await $.session.end({ reason: cancellation });
  finishFirst();
  expect((await first).text).toBe(cancellation === 'none'
    ? 'Showing the latest routing explanation.' : 'Could not open the native routing explanation pane.');
  const pane = await $.ui.mount({ ...panel, component: 'Pane' as const, requestId: 'smart-router-explanation' });
  expect(JSON.stringify(await pane.drawn())).toContain(cancellation === 'none'
    ? 'Selected for the task' : 'original Agent row');
  expect(placed).toBe(cancellation === 'none');
  expect(closes).toBe(cancellation === 'none' ? 0 : outcome === 'success' ? 2 : 1);
  await pane.unmount();
});
}
}

test('turning explanations off closes a pane that is still opening', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
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
    return { value: { isPlaced: true } };
  });

  on('ui.close', () => { placed = false; return { value: undefined }; });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'pane-opening', ...originalInput });
  const opening = $.command.run({ command: 'smart-router', args: 'explain latest' });
  await started;
  await $.command.run({ command: 'smart-router', args: 'explain off' });
  finishOpen();
  expect((await opening).text).toBe('Could not open the native routing explanation pane.');
  expect(placed).toBe(false);
});

test('an unplaced pane is rejected even when open returns normally', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
  let closes = 0;
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout:
    '{"model":"model-b","rationale":"Selected for the task"}',
  } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('ui.open', () => ({ value: { isPlaced: false, reason: 'no surface' } }));

  on('ui.close', () => { closes++; return { value: undefined }; });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'pane-placement', ...originalInput });
  const result = await $.command.run({ command: 'smart-router', args: 'explain latest' });
  expect(result.text).toBe('Could not open the native routing explanation pane.');
  expect(closes).toBe(1);
});

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
