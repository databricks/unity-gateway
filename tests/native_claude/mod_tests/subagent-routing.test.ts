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

test('parent reports recorded answers despite arbitrary prose, then resets on a fresh prompt', async ($, on) => {
  on('env.get', ($, e) => ({value: e.name === 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY' ? '1' : undefined}));
  const selectedModels: Record<string, string> = {
    'call-a': 'claude-sonnet-5',
    'call-b': 'claude-opus-4-8',
  };
  const inputs = ['call-a', 'call-b'].map((id) => ({
    tool: 'Agent' as const,
    tool_use_id: id,
    prompt: 'Inspect ' + id,
    description: 'Inspect ' + id,
    subagent_type: 'Explore',
    model: 'sonnet',
    run_in_background: true,
  }));
  let report = 'I used the requested model instead of the routed model.';
  const toolChunk = { kind: 'tool' as const, index: 2, id: 'read-one', name: 'Read' };
  const toolInputChunk = {
    kind: 'input' as const, index: 2, json: JSON.stringify({ file_path: 'README.md' }),
  };
  const toolUse = { name: 'Read', input: { file_path: 'README.md' } };
  const thinkingChunk = { kind: 'thinking' as const, index: 0, text: 'private thought' };
  let reportReady = false;
  const forwardedInputs: any[] = [];
  const handbacks: any[] = [];
  const forwardedSpawns: any[] = [];
  const submittedPrompts: any[] = [];
  const handbackFailures = new Map([
    ['handback-denied', 'deny'], ['handback-thrown', 'throw'],
  ] as const);
  const resumeFailures = new Map([
    ['child-call-a', 'deny'], ['child-call-b', 'throw'],
  ] as const);
  const finishToolCall = new Map<string, (result: any) => void>();
  let toolCallsReadyResolve: () => void = () => {};
  const toolCallsReady = new Promise<void>((resolve) => { toolCallsReadyResolve = resolve; });
  let finishSpawnA: ((result: any) => void) | undefined;
  let spawnAReadyResolve: () => void = () => {};
  const spawnAReady = new Promise<void>((resolve) => { spawnAReadyResolve = resolve; });

  on('session.id', () => ({ value: 'session-1' }));
  on('clock.every', () => ({ value: { cancel: () => {} } }));
  on('process.run', ($, event) => {
    const id = JSON.parse(event.init.stdin).tool_use_id;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: selectedModels[id], rationale: 'Selected for the task',
    }) } };
  });
  on('fs.write', () => ({ value: undefined }));
  on('agent.spawn', ($, event) => {
    forwardedSpawns.push(event);
    if (event.resume) {
      const failure = resumeFailures.get(event.resume.agentId);
      if (failure === 'deny') return { deny: 'Resume denied' };
      if (failure === 'throw') throw new Error('Resume failed');
    }
    if (event.tool_use_id === 'call-a') {
      return new Promise<any>((resolve) => {
        finishSpawnA = resolve;
        spawnAReadyResolve();
      });
    }
    return { agentId: 'child-' + event.tool_use_id, model: event.model };
  });
  on('tool.call', ($, event) => {
    if (event.tool === 'SubagentHandback') {
      handbacks.push(event);
      const failure = handbackFailures.get(event.tool_use_id);
      if (failure === 'deny') return { deny: 'Handback denied' };
      if (failure === 'throw') throw new Error('Handback failed');
      return { result: 'Handback recorded' };
    }
    forwardedInputs.push(event);
    return new Promise<any>((resolve) => {
      finishToolCall.set(event.tool_use_id, resolve);
      if (finishToolCall.size === inputs.length) toolCallsReadyResolve();
    });
  });
  on('turn.complete', () => ({ text: '' }));
  on('prompt.submit', ($, event) => {
    submittedPrompts.push(event);
    return { text: event.text };
  });
  on('turn.step', async function* ($, event) {
    if (event.turnId === 'parent-report') {
      if (!reportReady) {
        return {
          turnId: event.turnId, index: event.index, answer: '',
          toolUses: [], stopReason: null, usage: null,
        };
      }
      const split = Math.floor(report.length / 2);
      yield thinkingChunk;
      if (report) yield { kind: 'text', index: 1, text: report.slice(0, split) };
      yield toolChunk;
      yield toolInputChunk;
      if (report) yield { kind: 'text', index: 3, text: report.slice(split) };
      yield {kind: 'stop', stopReason: 'tool_use', usage: null};
      return {
        turnId: event.turnId, index: event.index, answer: report,
        toolUses: [toolUse], stopReason: 'tool_use', usage: null,
      };
    }
    if (event.turnId === 'notification-turn') {
      const status = JSON.stringify({ child_id: 'child-call-a', view: 'status' });
      yield { kind: 'text', index: 0, text: status };
      return {
        turnId: event.turnId, index: event.index, answer: status,
        toolUses: [], stopReason: 'end_turn', usage: null,
      };
    }
    const answer = 'A regular main-only answer.';
    yield { kind: 'text', index: 0, text: answer };
    return {
      turnId: event.turnId, index: event.index, answer,
      toolUses: [], stopReason: 'end_turn', usage: null,
    };
  });

  const spawnArgs = (id: string) => {
    const input = inputs.find((candidate) => candidate.tool_use_id === id)!;
    return {
      tool_use_id: id, prompt: input.prompt, description: input.description,
      subagentType: input.subagent_type, parentModel: 'parent-model',
      provider: { plugin: 'engine', tier: 'core' as const }, background: true, fork: false,
      name: 'caller-' + id, permissionMode: 'default', parentAgentId: undefined, cwd: '/workspace',
    };
  };

  expect((await collectParentTurn($, 'parent-report', 0)).text).toBe('');
  const pendingToolCalls = inputs.map((input) => $.tool.call(input));
  await toolCallsReady;
  const pendingSpawnA = $.agent.spawn(spawnArgs('call-a'));
  await spawnAReady;
  const childB = await $.agent.spawn(spawnArgs('call-b'));
  const handedBackAnswer = 'Handed-back answer from child B';
  await $.tool.call({
    tool: 'SubagentHandback', agentId: 'child-call-b', tool_use_id: 'handback-b', message: handedBackAnswer,
  });
  expect(handbacks).toEqual([{
    tool: 'SubagentHandback', agentId: 'child-call-b', tool_use_id: 'handback-b', message: handedBackAnswer,
  }]);
  reportReady = true;
  const running = await collectParentTurn($, 'parent-report', 1);
  expect(running.text).toBe('');
  expect(running.result.answer).toBe('');
  expect(running.chunks).toEqual([toolChunk, toolInputChunk, {kind: 'stop', stopReason: 'tool_use', usage: null}]);
  expect(running.result.toolUses).toEqual([toolUse]);
  await $.turn.complete({
    agentId: 'child-call-b', turnId: 'child-turn-b', answer: 'Original answer from child B',
    reason: 'answer', isAborted: false, durationMs: 1,
    usage: { model: 'provider-backend-revision' } as any,
  });
  await $.turn.complete({
    agentId: 'child-call-a', turnId: 'child-turn-a', answer: 'Answer from child A',
    reason: 'answer', isAborted: false, durationMs: 1,
  });
  finishSpawnA!({ agentId: 'child-call-a', model: selectedModels['call-a'] });
  const childA = await pendingSpawnA;
  expect(childA).toEqual({ agentId: 'child-call-a', model: selectedModels['call-a'] });
  expect(childB).toEqual({ agentId: 'child-call-b', model: selectedModels['call-b'] });
  expect(forwardedInputs).toEqual(inputs);
  expect(forwardedSpawns).toEqual([
    { ...spawnArgs('call-a'), model: selectedModels['call-a'] },
    { ...spawnArgs('call-b'), model: selectedModels['call-b'] },
  ]);
  finishToolCall.get('call-a')!({ result: { agentId: 'child-call-a' }, text: 'Child A launched' });
  finishToolCall.get('call-b')!({ result: { agentId: 'child-call-b' }, text: 'Child B launched' });
  const launchResults = await Promise.all(pendingToolCalls);
  expect(launchResults[0].context.join('\n')).toContain('"launched_model":"claude-sonnet-5"');
  expect(launchResults[1].context.join('\n')).toContain('"launched_model":"claude-opus-4-8"');

  reportReady = true;
  const parent = await collectParentTurn($, 'parent-report', 1);
  expect(parent.text).toBe(parent.result.answer);
  expect(parent.text).toMatch(/^[A-Z][a-z]+(?: \d+)?\n\nChild answer:\n> Answer from child A$/m);
  expect(parent.text).toMatch(/^[A-Z][a-z]+(?: \d+)?\n\nChild answer:\n> Handed-back answer from child B$/m);
  expect(parent.text).not.toContain(' · ');
  expect(parent.text).toContain('Child answer:\n> ' + handedBackAnswer);
  expect(parent.text).not.toContain('Original answer from child B');
  expect(parent.text).not.toContain(report);
  expect(parent.text).toContain('Child answer:\n> Answer from child A');
  expect(parent.chunks.find((chunk) => chunk.kind === 'tool')).toEqual(toolChunk);
  expect(parent.chunks.find((chunk) => chunk.kind === 'input')).toEqual(toolInputChunk);
  expect(parent.chunks.some((chunk) => chunk.kind === 'thinking')).toBe(false);
  expect(parent.text).not.toContain('private thought');
  expect(parent.result.toolUses).toEqual([toolUse]);

  report = '';
  const noText = await collectParentTurn($, 'parent-report', 2);
  expect(noText.text).toContain('Child answer:\n> ' + handedBackAnswer);
  expect(noText.result.answer).toBe(noText.text);
  expect(noText.chunks[noText.chunks.length - 1].kind).toBe('stop');
  expect(noText.chunks[noText.chunks.length - 2].kind).toBe('text');
  expect(noText.result.toolUses).toEqual([toolUse]);

  report = JSON.stringify([
    { child_id: 'child-call-a', view: 'result' },
    { child_id: 'child-call-b', view: 'result' },
  ]);
  const deniedHandback = await $.tool.call({
    tool: 'SubagentHandback', agentId: 'child-call-b', tool_use_id: 'handback-denied',
    message: 'Denied replacement answer',
  });
  expect(deniedHandback).toEqual({ deny: 'Handback denied' });
  const afterDeniedHandback = await collectParentTurn($, 'parent-report', 2);
  expect(afterDeniedHandback.text).toContain('Child answer:\n> ' + handedBackAnswer);
  expect(afterDeniedHandback.text).not.toContain('Denied replacement answer');

  let handbackThrew = false;
  try {
    await $.tool.call({
      tool: 'SubagentHandback', agentId: 'child-call-b', tool_use_id: 'handback-thrown',
      message: 'Thrown replacement answer',
    });
  } catch {
    handbackThrew = true;
  }
  expect(handbackThrew).toBe(true);
  const afterThrownHandback = await collectParentTurn($, 'parent-report', 3);
  expect(afterThrownHandback.text).toContain('Child answer:\n> ' + handedBackAnswer);
  expect(afterThrownHandback.text).not.toContain('Thrown replacement answer');

  const deniedResume = await $.agent.spawn({
    ...spawnArgs('call-a'), resume: { agentId: 'child-call-a' },
  });
  expect(deniedResume).toEqual({ deny: 'Resume denied' });
  const afterDeniedResume = await collectParentTurn($, 'parent-report', 4);
  expect(afterDeniedResume.text).toContain('Child answer:\n> Answer from child A');
  expect(afterDeniedResume.text).toContain('Child answer:\n> ' + handedBackAnswer);

  let resumeThrew = false;
  try {
    await $.agent.spawn({ ...spawnArgs('call-b'), resume: { agentId: 'child-call-b' } });
  } catch {
    resumeThrew = true;
  }
  expect(resumeThrew).toBe(true);
  const afterThrownResume = await collectParentTurn($, 'parent-report', 5);
  expect(afterThrownResume.text).toContain('Child answer:\n> Answer from child A');
  expect(afterThrownResume.text).toContain('Child answer:\n> ' + handedBackAnswer);
  await $.turn.complete({
    turnId: 'parent-report', answer: afterThrownResume.result.answer, reason: 'answer',
    isAborted: false, durationMs: 1,
  });

  await $.prompt.submit({
    text: 'A background task update', wait: false, origin: { kind: 'task-notification' },
  } as any);
  const forwardedNotification = submittedPrompts.find((event) => event.text === 'A background task update');
  expect(forwardedNotification.context).toBeUndefined();
  const notification = await collectParentTurn($, 'notification-turn', 0);
  expect(notification.text).toContain('Child answer:\n> Answer from child A');
  expect(notification.text).not.toContain(' · ');
  expect(notification.text).toBe(notification.result.answer);
  await $.turn.complete({
    turnId: 'notification-turn', answer: notification.result.answer, reason: 'answer',
    isAborted: false, durationMs: 1,
  });

  await $.prompt.submit({
    text: 'An unrelated task update after completed reports were consumed.', wait: false,
    origin: { kind: 'task-notification' },
  } as any);
  const unrelatedForwardedNotification = submittedPrompts.find((event) =>
    event.text === 'An unrelated task update after completed reports were consumed.');
  expect(unrelatedForwardedNotification.context).toBeUndefined();
  const unrelatedNotification = await collectParentTurn($, 'unrelated-notification', 0);
  expect(unrelatedNotification.text).toBe('A regular main-only answer.');
  expect(unrelatedNotification.result.answer).toBe(unrelatedNotification.text);
  await $.turn.complete({
    turnId: 'unrelated-notification', answer: unrelatedNotification.text, reason: 'answer',
    isAborted: false, durationMs: 1,
  });

  await $.prompt.submit({ text: 'fresh task', wait: false, origin: { kind: 'sdk' } });
  const mainOnly = await collectParentTurn($, 'main-only', 0);
  expect(mainOnly.text).toBe('A regular main-only answer.');
  expect(mainOnly.result.answer).toBe(mainOnly.text);
  await $.turn.complete({
    turnId: 'main-only', answer: mainOnly.text, reason: 'answer',
    isAborted: false, durationMs: 1,
  });
});

test('parent prose and stale references cannot override mismatched launch records', async ($, on) => {
  on('env.get', ($, e) => ({value: e.name === 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY' ? '1' : undefined}));
  const selected = 'claude-sonnet-5';
  const actual = 'claude-opus-4-8';
  const unavailable = 'Smart Router report unavailable: the child model could not be verified.';
  const interrupted = 'Smart Router report interrupted: the model response did not complete.';
  const partialWrongText = 'I started a Luna subagent and it found the answer.';
  const frames: Array<{ text: string; expected: string; streamError?: boolean }> = [
    { text: 'I started a Fox subagent.', expected: unavailable },
    { text: JSON.stringify({ child_id: 'child-one', view: 'status', model: actual }), expected: unavailable },
    { text: JSON.stringify({ child_id: 'old-child', view: 'status' }), expected: unavailable },
    { text: JSON.stringify({ child_id: 'child-one', view: 'status' }), expected: unavailable },
    { text: JSON.stringify({ child_id: 'child-one', view: 'result' }), expected: unavailable },
    { text: partialWrongText, expected: interrupted, streamError: true },
  ];
  const forwardedSpawns: any[] = [];
  const submittedPrompts: any[] = [];
  let finishToolCall: ((result: any) => void) | undefined;
  let toolCallStartedResolve: () => void = () => {};
  const toolCallStarted = new Promise<void>((resolve) => { toolCallStartedResolve = resolve; });
  let promptSubmittedResolve: () => void = () => {};
  const promptSubmitted = new Promise<void>((resolve) => { promptSubmittedResolve = resolve; });
  let finishQueuedPrompt: ((result: any) => void) | undefined;

  on('session.id', () => ({ value: 'session-1' }));
  on('clock.every', () => ({ value: { cancel: () => {} } }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
    model: selected, rationale: 'Selected for the task',
  }) } }));
  on('fs.write', () => ({ value: undefined }));
  on('agent.spawn', ($, event) => {
    forwardedSpawns.push(event);
    return { agentId: 'child-one', model: actual };
  });
  on('tool.call', () => new Promise<any>((resolve) => {
    finishToolCall = resolve;
    toolCallStartedResolve();
  }));
  on('turn.complete', () => ({ text: '' }));
  on('prompt.submit', ($, event) => new Promise<any>((resolve) => {
    submittedPrompts.push(event);
    finishQueuedPrompt = resolve;
    promptSubmittedResolve();
  }));
  on('turn.step', async function* ($, event) {
    if (event.turnId === 'queued-user-turn') {
      const answer = 'A fresh user-task answer.';
      yield { kind: 'text', index: 0, text: answer };
      return {
        turnId: event.turnId, index: event.index, answer,
        toolUses: [], stopReason: 'end_turn', usage: null,
      };
    }
    if (event.turnId === 'parent-invalid' && event.index === 0) {
      return {
        turnId: event.turnId, index: event.index, answer: '',
        toolUses: [], stopReason: null, usage: null,
      };
    }
    const frame = frames[event.index - 1];
    const answer = frame.text;
    if (frame.streamError) {
      yield { kind: 'text', index: 0, text: answer.slice(0, 24) };
      throw new Error('synthetic native stream failure');
    }
    const split = Math.floor(answer.length / 2);
    yield { kind: 'text', index: 0, text: answer.slice(0, split) };
    yield { kind: 'text', index: 1, text: answer.slice(split) };
    return {
      turnId: event.turnId, index: event.index, answer,
      toolUses: [], stopReason: 'end_turn', usage: null,
    };
  });

  const input = {
    tool: 'Agent' as const, tool_use_id: 'call-one', prompt: 'Inspect the parser',
    description: 'Inspect the parser', subagent_type: 'Explore', model: 'sonnet',
    run_in_background: true,
  };
  const spawnArgs = {
    tool_use_id: 'call-one', prompt: input.prompt, description: input.description,
    subagentType: input.subagent_type, parentModel: 'parent-model',
    provider: { plugin: 'engine', tier: 'core' as const }, background: true, fork: false,
    name: 'caller-one', permissionMode: 'default', parentAgentId: undefined, cwd: '/workspace',
  };
  expect((await collectParentTurn($, 'parent-invalid', 0)).text).toBe('');
  const pendingToolCall = $.tool.call(input);
  await toolCallStarted;
  const child = await $.agent.spawn(spawnArgs);
  expect(child).toEqual({ agentId: 'child-one', model: actual });
  expect(forwardedSpawns).toEqual([{ ...spawnArgs, model: selected }]);
  finishToolCall!({ result: { agentId: child.agentId }, text: 'Child launched' });
  const launch = await pendingToolCall;
  const receipt = launch.context.join('\n');
  expect(receipt).toContain('"selected_model":"claude-sonnet-5"');
  expect(receipt).toContain('"launched_model":"claude-opus-4-8"');
  let queuedPromptResolved = false;
  const queuedPrompt = $.prompt.submit({
    text: 'Check this after the delegated work.', turnId: 'queued-user-turn', wait: true,
    origin: { kind: 'composer' },
  } as any).then((result: any) => { queuedPromptResolved = true; return result; });
  await promptSubmitted;
  expect(queuedPromptResolved).toBe(false);
  expect(submittedPrompts[0]).toMatchObject({
    text: 'Check this after the delegated work.', turnId: 'queued-user-turn', wait: true,
  });
  await $.turn.complete({
    agentId: 'child-one', turnId: 'child-one-turn', answer: 'Verified child answer',
    reason: 'answer', isAborted: false, durationMs: 1,
    usage: { model: selected } as any,
  });

  for (let index = 1; index <= frames.length; index++) {
    const frame = await collectParentTurn($, 'parent-invalid', index);
    expect(frame.text).toBe(frames[index - 1].expected);
    expect(frame.result.answer).toBe(frames[index - 1].expected);
    expect(frame.text).not.toContain(frames[index - 1].text);
    if (index === 1) expect(queuedPromptResolved).toBe(false);
    if (frames[index - 1].streamError) {
      expect(frame.text).not.toContain(partialWrongText.slice(0, 24));
      expect(frame.result.stopReason).toBeNull();
      expect(frame.result.usage).toBeNull();
      expect(frame.result.toolUses).toEqual([]);
    }
  }
  await $.turn.complete({
    turnId: 'parent-invalid', answer: frames[frames.length - 1].expected, reason: 'answer',
    isAborted: false, durationMs: 1,
  });
  finishQueuedPrompt!({ text: 'Check this after the delegated work.' });
  expect((await queuedPrompt).text).toBe('Check this after the delegated work.');
  const queuedTurn = await collectParentTurn($, 'queued-user-turn', 0);
  expect(queuedTurn.text).toBe('A fresh user-task answer.');
  expect(queuedTurn.result.answer).toBe(queuedTurn.text);
  await $.turn.complete({
    turnId: 'queued-user-turn', answer: queuedTurn.text, reason: 'answer',
    isAborted: false, durationMs: 1,
  });
});


test('a spawn step suppresses premature identity prose while keeping reviewed tool inputs', async ($, on) => {
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
  expect(spawn.text).toBe('');
  expect(spawn.result.answer).toBe('');
  expect(spawn.result.toolUses).toEqual(tools);
  expect(spawn.chunks.map(chunk => chunk.kind)).toEqual(['tool', 'input', 'tool', 'input']);
  expect(spawn.chunks[1].json).toBe(JSON.stringify(originalInput));
});
