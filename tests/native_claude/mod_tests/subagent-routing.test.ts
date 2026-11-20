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

for (const staticModel of [undefined, 'ug-auto-test']) {
  test(`Auto hides routing details for lone and parallel calls (${staticModel ? 'static' : 'custom'} picker)`, {
    options: {
      ENABLE_SMART_ROUTER_AUTO_MODE: '1',
      ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI: '0',
      ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
      autoPicker: JSON.stringify({
        model: 'ug-auto-test', mainModel: 'system.ai.claude-sonnet-5', staticModel,
        models: [{ model: 'system.ai.claude-sonnet-5', label: 'Sonnet', autoModel: 'ug-auto-test' }],
      }),
    },
  }, async ($, on) => {
    let release = () => {};
    let routing = Promise.resolve();
    const spawns: string[] = [];
    const labels: string[] = [];
    on('session.id', () => ({ value: 'session-auto' }));
    on('session.model', () => ({ value: 'ug-auto-test' }));
    on('env.get', ($, e) => ({ value: e.name === 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY' ? '1' : e.name === 'ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI' ? '0' : undefined }));
    on('clock.now', () => ({ value: 10_000 }));
    on('clock.every', () => ({ value: { cancel: () => {} } }));
    on('process.run', async () => {
      await routing;
      return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
        model: 'system.ai.glm-5-3-flash', rationale: 'Private routing reason',
      }) } };
    });
    on('agent.spawn', ($, e) => {
      spawns.push(e.model);
      return { agentId: `child-${e.tool_use_id}`, model: 'system.ai.glm-5-3-flash' };
    });
    on('fs.write', ($, e) => { labels.push(e.path); return { value: undefined }; });
    on('tool.call', () => ({ result: 'ok' }));
    on('ui.render', ($, e) => ({ type: 'Text', props: {}, children: [e.component === 'Spinner' ? e.props.message ?? e.props.word : 'Native content'] }));
    const hidden = /Selected Model|Launched Model|Reason:|glm-5-3-flash|Private routing reason|routed in/;
    const spinner = { ...panel, component: 'Spinner' as const, requestId: 'auto-spinner', props: { word: 'Working', message: null, suffix: '…', mode: 'tool-use' as const } };
    const check = async (event: Parameters<typeof $.ui.mount>[0]) => {
      const ui = await $.ui.mount(event);
      expect(await ui.find({ type: 'Text', text: hidden })).toBeUndefined();
      if (event.component === 'Spinner') {
        // The in-progress indicator names no model, so blind configs still show it.
        expect(await ui.find({ type: 'Text', text: /Selecting a model|Selecting models for/ })).toBeDefined();
      } else {
        expect(await ui.find({ type: 'Text', text: 'Native content' })).toBeDefined();
      }
      await ui.unmount();
    };
    for (const ids of [['lone'], ['parallel-a', 'parallel-b']]) {
      routing = new Promise<void>(resolve => { release = resolve; });
      const calls = ids.map(id => $.tool.call({ tool: 'Agent', tool_use_id: id, ...originalInput }));
      for (const id of ids) await check(row(id));
      await check(spinner);
      release();
      await Promise.all(calls);
      for (const id of ids) {
        await $.agent.spawn({
          tool_use_id: id, prompt: originalInput.prompt, description: originalInput.description,
          subagentType: 'Explore', parentModel: 'ug-auto-test',
          provider: { plugin: 'engine', tier: 'core' }, background: false, fork: false,
          name: id, permissionMode: 'default', parentAgentId: 'main', cwd: '/workspace',
        });
        await check(row(id));
      }
      await check(reply(`after-${ids[0]}`));
    }
    expect(spawns).toEqual(['system.ai.glm-5-3-flash', 'system.ai.glm-5-3-flash', 'system.ai.glm-5-3-flash']);
    expect(labels).toEqual([]);
  });
}

for (const [show, auto] of [['0', false], ['1', true], ['1', false]] as const) {
  test(`the display flag controls routing details independently of Auto (${show}, ${auto})`, {
    options: {
      ENABLE_SMART_ROUTER_AUTO_MODE: auto ? '1' : '0',
      ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI: show,
      autoPicker: auto ? JSON.stringify({
        model: 'ug-auto-test', mainModel: 'system.ai.claude-sonnet-5',
        models: [{ model: 'system.ai.claude-sonnet-5', label: 'Sonnet', autoModel: 'ug-auto-test' }],
      }) : '',
    },
  }, async ($, on) => {
    on('session.id', () => ({ value: 'session-display' }));
    on('session.model', () => ({ value: 'ug-auto-test' }));
    on('env.get', ($, e) => ({ value: e.name === 'ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI' ? show : e.name === 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY' ? '1' : undefined }));
    let release = () => {};
    const routing = new Promise<void>(resolve => { release = resolve; });
    on('process.run', async () => {
      await routing;
      return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
        model: 'system.ai.glm-5-3-flash', rationale: 'Display flag reason',
      }) } };
    });
    on('tool.call', () => ({ result: 'ok' }));
    on('ui.render', ($, e) => ({ type: 'Text', props: {}, children: [e.component === 'Spinner' ? e.props.message ?? e.props.word : 'Native content'] }));
    const logs: string[] = [];
    on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
    on('agent.spawn', () => ({ agentId: 'child-without-model', model: '' }));
    const call = $.tool.call({ tool: 'Agent', tool_use_id: 'display-call', ...originalInput });
    // While routing is under way the spinner says so, whatever the display flag says;
    // only the decided model details follow the flag.
    const spinner = { ...panel, component: 'Spinner' as const, requestId: 'display-spinner', props: { word: 'Working', message: null, suffix: '…', mode: 'tool-use' as const } };
    const spinnerUi = await $.ui.mount(spinner);
    expect(await spinnerUi.find({ type: 'Text', text: 'Selecting a model' })).toBeDefined();
    await spinnerUi.unmount();
    release();
    await call;
    const ui = await $.ui.mount(row('display-call'));
    expect(await ui.find({ type: 'Text', text: 'Native content' })).toBeDefined();
    for (const text of ['  ↳ Selected Model: ', 'glm-5-3-flash', 'Display flag reason']) {
      expect(Boolean(await ui.find({ type: 'Text', text }))).toBe(show === '1');
    }
    await ui.unmount();
    await $.agent.spawn({
      tool_use_id: 'display-call', prompt: originalInput.prompt, description: originalInput.description,
      subagentType: 'Explore', parentModel: 'parent', provider: { plugin: 'engine', tier: 'core' },
      background: false, fork: false, permissionMode: 'default', cwd: '/workspace',
    });
    expect(logs).toEqual(show === '1'
      ? ['Claude did not report the launched model; routed labels cannot be verified for this subagent.'] : []);
  });
}

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
      expect(displayed).toEqual({ ...event.props, tool: name, input: { ...input,
        description: `${name} · claude-haiku-5-5 · inspect the parser` } });
      expect(event.props.input).toEqual(input);
      expect(await mounted.find({ type: 'Text', text: /Backgrounded agent/ })).toBeDefined();
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

test('routing changes the matching spawn model while preserving the task and its address', async ($, on) => {
  const writes: any[] = [];
  const forwarded: any[] = [];
  const shown = new Map<string, string>();
  let failWrite = false;
  const logs: string[] = [];
  const routedModel = 'system.ai.gpt-5-6-sol[1m]';
  on('session.id', () => ({ value: 'session-1' }));
  on('env.get', ($, e) => ({ value: e.name === 'UCODE_AGENT_LABELS_DIR' ? '/plugin-data' : undefined }));
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
  const displayInputs: any[] = [];
  on('ui.render', ($, e) => {
    shown.set(e.requestId, e.props.tool);
    displayInputs.push(e.props.input);
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
    // A routed spawn carries the label the task will show; an unrouted one is untouched.
    expect(forwarded[forwarded.length - 1]).toEqual(id === 'unrouted' ? spawn
      : { ...spawn, model: routedModel, description: `${shown.get(id)} · gpt-5-6-sol · inspect the parser` });
    expect(result).toEqual(id === 'denied' ? { deny: 'Permission denied' }
      : { model: 'claude-sonnet-5', agentId: `child-${id}` });
  }
  const launched = await $.ui.mount(row('call-a'));
  expect(await launched.find({ type: 'Text', text: '⚠ Launched Model: claude-sonnet-5 (differs from the selected model)' })).toBeDefined();
  expect(displayInputs[displayInputs.length - 1].description).toMatch(/^[A-Z][a-z]+ · claude-sonnet-5 · inspect the parser ⚠ selected gpt-5-6-sol$/);
  await launched.unmount();
  const denied = await $.ui.mount(row('denied'));
  expect(await denied.find({ type: 'Text', text: /Launched Model:/ })).toBeUndefined();
  await denied.unmount();
  expect(writes.length).toBe(4);
  const visibleWrites = writes.filter(write => write.path.includes('/agent-names/'));
  const persistentWrites = writes.filter(write => write.path.startsWith('/plugin-data/'));
  expect(visibleWrites.length).toBe(2);
  expect(persistentWrites.length).toBe(2);
  for (const [index, id] of ['call-b', 'call-a'].entries()) {
    expect(visibleWrites[index].path.endsWith(`/agent-names/agent-child-${id}.json`)).toBe(true);
    expect(persistentWrites[index].text).toBe(visibleWrites[index].text);
    expect(JSON.parse(visibleWrites[index].text)).toEqual({
      session_id: 'session-1', agent_id: `child-${id}`, name: `${shown.get(id)} — claude-sonnet-5 ⚠ selected gpt-5-6-sol`,
      description: originalInput.description, animal: shown.get(id), title: 'inspect the parser',
      selected: routedModel, launched: 'claude-sonnet-5',
    });
  }
  const resumed = { ...forwarded[0], description: originalInput.description,
    model: 'existing-child-model', resume: { agentId: 'child-call-b' } };
  await $.agent.spawn(resumed);
  expect(forwarded[forwarded.length - 1]).toEqual(resumed);
  expect(writes.length).toBe(4);
  failWrite = true;
  const result = await $.agent.spawn(forwarded[0]);
  expect(result).toEqual({ model: 'claude-sonnet-5', agentId: 'child-call-b' });
  expect(writes.length).toBe(4);
  expect(logs).toEqual(['Could not persist the subagent display label; native labels remain where unavailable.']);
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
    expect(await done.find({ type: 'Text', text: originalInput.description })).toBeUndefined();
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
  expect(tree).not.toContain('Why this model?');
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
  expect(await final.findAll({ type: 'Text', text: '↳ Selected Model: ' })).toHaveLength(2);
  expect(await final.find({ type: 'Text', text: 'claude-sonnet-5' })).toBeDefined();
  expect(await final.find({ type: 'Text', text: /Selected for call-a/ })).toBeDefined();
  expect(await final.find({ type: 'Text', text: /Selected for call-b/ })).toBeDefined();
  await final.unmount();
});

test('the router hears which subagent made the call, and nothing for the main loop', async ($, on) => {
  const payloads: any[] = [];
  const calls: any[] = [];
  const spawns: any[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', ($, e) => {
    const payload = JSON.parse(e.init.stdin);
    payloads.push(payload);
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'system.ai.claude-sonnet-5', rationale: 'Small task',
    }) } };
  });
  on('tool.call', ($, e) => { calls.push(e); return { result: 'ok' }; });
  on('agent.spawn', ($, e) => { spawns.push(e); return { model: 'claude-sonnet-5', agentId: `child-${e.tool_use_id}` }; });
  on('fs.write', () => ({ value: undefined }));
  on('ui.log', () => ({ value: undefined }));
  await $.tool.call({ tool: 'Agent', tool_use_id: 'call-main', ...originalInput });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'call-nested', agentId: 'agent-parent', ...originalInput });
  expect('agent_id' in payloads[0]).toBe(false);
  expect(payloads[0].tool_input).toEqual(originalInput);
  expect(payloads[1].agent_id).toBe('agent-parent');
  expect(payloads[1].tool_input).toEqual(originalInput);
  // The reviewed call is forwarded untouched; the model is applied at agent.spawn.
  expect(calls).toEqual([
    { tool: 'Agent', tool_use_id: 'call-main', ...originalInput },
    { tool: 'Agent', tool_use_id: 'call-nested', agentId: 'agent-parent', ...originalInput },
  ]);
  const spawn = (tool_use_id: string, parentAgentId: string) => ({
    tool_use_id, prompt: originalInput.prompt, description: originalInput.description,
    subagentType: originalInput.subagent_type, parentModel: 'claude-opus-4-8',
    provider: { plugin: 'engine', tier: 'core' as const }, background: false, fork: false,
    name: 'caller-address', permissionMode: 'default' as const,
    parentAgentId, cwd: '/workspace',
  });
  await $.agent.spawn(spawn('call-main', 'main'));
  await $.agent.spawn(spawn('call-nested', 'agent-parent'));
  // The reviewed call is untouched; the spawn carries the model and the task's label.
  const [{ description: _mainLabel, ...mainRest }, { description: _nestedLabel, ...nestedRest }] = spawns;
  const { description: _mainWritten, ...mainExpected } = spawn('call-main', 'main');
  const { description: _nestedWritten, ...nestedExpected } = spawn('call-nested', 'agent-parent');
  expect(mainRest).toEqual({ ...mainExpected, model: 'system.ai.claude-sonnet-5' });
  expect(nestedRest).toEqual({ ...nestedExpected, model: 'system.ai.claude-sonnet-5' });
  for (const spawnEvent of spawns) expect(spawnEvent.description)
    .toMatch(/^[A-Z][a-z]+(?: 2)? · claude-sonnet-5 · inspect the parser$/);
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
    const { description, ...forwarded } = forwardedSpawns[forwardedSpawns.length - 1];
    const { description: _originalDescription, ...original } = spawn;
    expect(forwarded).toEqual({ ...original, model: selected[input.tool_use_id] });
    // The spawn description becomes the routed label, after the permission decision.
    expect(description).toMatch(new RegExp(
      `^[A-Z][a-z]+(?: 2)? · ${selected[input.tool_use_id]} · Inspect ${input.tool_use_id}$`));
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

test('routed joke titles use the selected model without rewriting reviewed arguments', async ($, on) => {
  const forwarded: any[] = [];
  const spawned: any[] = [];
  const displayed = new Map<string, any>();
  const gates: (() => void)[] = [];
  let ready: () => void = () => {};
  const started = new Promise<void>(resolve => { ready = resolve; });
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', async () => {
    await new Promise<void>(resolve => { gates.push(resolve); if (gates.length === 3) ready(); });
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      model: 'catalog.schema.routed-model', rationale: 'Selected for this task.',
    }) } };
  });
  on('tool.call', ($, e) => { forwarded.push(e); return { result: 'launched' }; });
  on('agent.spawn', ($, e) => {
    spawned.push(e);
    return { agentId: `child-${e.tool_use_id}`, model: e.model };
  });
  on('ui.render', ($, e) => {
    displayed.set(e.requestId, e.props);
    return { type: 'Text', props: {}, children: [
      e.component === 'ToolUse' ? String(e.props.input?.description ?? 'native row') : 'parent answer',
    ] };
  });
  const calls = ['Sonnet', 'Haiku', 'Opus'].map((model, index) => ({
    tool: 'Agent' as const, tool_use_id: `joke-${index}`, ...originalInput,
    description: index === 0 ? 'Joke from "Sonnet" now\nplease' : `Joke from ${model}`, name: `address-${model}`, run_in_background: true,
  }));
  const pending = calls.map(call => $.tool.call(call));
  await started;
  for (const release of gates) release();
  await Promise.all(pending);
  expect(forwarded).toEqual(calls);
  const cards = await $.ui.mount(reply('jokes'));
  expect(await cards.findAll({ type: 'Text', text: '↳ Selected Model: ' })).toHaveLength(3);
  expect(await cards.findAll({ type: 'Text', text: / — routed-model \(selected, launch pending\)$/ })).toHaveLength(3);
  for (const call of calls) {
    // The card names the neutral task, never the model the description asked for.
    expect(await cards.find({ type: 'Text', text: `Task request: inspect the parser` })).toBeDefined();
    expect(await cards.find({ type: 'Text', text: call.description })).toBeUndefined();
    const event = row(call.tool_use_id);
    const pendingRow = await $.ui.mount({ ...event, props: { ...event.props, input: call } });
    expect(displayed.get(call.tool_use_id).input).toEqual({ ...call,
      description: `${displayed.get(call.tool_use_id).tool} · routed-model · inspect the parser` });
    await pendingRow.unmount();
    const spawn = {
      tool_use_id: call.tool_use_id, prompt: call.prompt, description: call.description,
      subagentType: call.subagent_type, parentModel: 'parent-model',
      provider: { plugin: 'engine', tier: 'core' as const }, background: true, fork: false,
      name: call.name, permissionMode: 'default', parentAgentId: undefined, cwd: '/workspace',
    };
    await $.agent.spawn(spawn);
    const ui = await $.ui.mount({ ...event, props: { ...event.props, input: call } });
    // The rendered label comes from the launch receipt without changing the call.
    expect(displayed.get(call.tool_use_id).input).toEqual({ ...call,
      description: `${displayed.get(call.tool_use_id).tool} · routed-model · inspect the parser` });
    const label = `${displayed.get(call.tool_use_id).tool} — routed-model`;
    const { description: _spawned, ...spawnRest } = spawn;
    expect(spawned[spawned.length - 1]).toEqual({
      ...spawnRest, model: 'catalog.schema.routed-model',
      description: `${displayed.get(call.tool_use_id).tool} · routed-model · inspect the parser`,
    });
    expect(await cards.find({ type: 'Text', text: label })).toBeDefined();
    await ui.unmount();
  }
  // Every forwarded spawn carries the routed label; the reviewed call input stays as written.
  for (const spawn of spawned) expect(spawn.description)
    .toMatch(/^[A-Z][a-z]+(?: 2)? · routed-model · inspect the parser$/);
  // The completed notice quotes the routed label in its heading, never the description;
  // the summary after it stays as written.
  const notice = { ...panel, component: 'UserMessage' as const, requestId: 'notice-child-joke-0',
    props: { text: `Agent "Joke from "Sonnet" now\nplease" finished\nSummary: Joke from Sonnet`, origin: { kind: 'task-notification' as const },
      task: { id: 'child-joke-0', status: 'completed', toolUseId: 'joke-0', durationMs: 1 }, isExpanded: false } };
  const reported = await $.ui.mount(notice);
  expect(displayed.get('notice-child-joke-0').text)
    .toMatch(/^Agent "[A-Z][a-z]+(?: 2)? · routed-model · inspect the parser" finished\nSummary: Joke from Sonnet$/);
  await reported.unmount();
  const otherVerb = await $.ui.mount({ ...notice, props: { ...notice.props,
    text: notice.props.text.replace('finished', 'concluded') } });
  expect(displayed.get('notice-child-joke-0').text)
    .toMatch(/^Agent "[A-Z][a-z]+(?: 2)? · routed-model · inspect the parser" concluded\nSummary: Joke from Sonnet$/);
  await otherVerb.unmount();
  for (const text of [42, 'Agent "other task" finished\nSummary: Joke from Sonnet']) {
    const untouched = await $.ui.mount({ ...notice, props: { ...notice.props, text: text as any } });
    expect(displayed.get('notice-child-joke-0').text).toBe(text);
    await untouched.unmount();
  }
  for (const input of [undefined, null, false, 42, 'native input', []]) {
    const event = row(calls[0].tool_use_id);
    const ui = await $.ui.mount({ ...event, props: { ...event.props, input } });
    expect(displayed.get(calls[0].tool_use_id).input).toEqual(input);
    await ui.unmount();
  }
  await cards.unmount();
});

test('hidden model display does not restore saved native labels', {
  options: { ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI: '0' },
}, async ($, on) => {
  const writes: string[] = [];
  const renders: any[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('env.get', ($, e) => ({ value: e.name === 'UCODE_AGENT_LABELS_DIR' ? '/plugin-data' : undefined }));
  on('env.set', () => ({ value: undefined }));
  on('session.start', ($, e) => ({ cwd: e.cwd }));
  on('fs.exists', () => ({ value: true }));
  on('fs.list', () => ({ value: [{ name: 'agent-child.json', kind: 'file', size: 100, mtimeMs: 0, isLink: false }] }));
  on('fs.read', () => ({ value: JSON.stringify({
    session_id: 'session-1', agent_id: 'child', name: 'Fox · model-b', description: 'Inspect parser',
    animal: 'Fox', title: 'Inspect parser', selected: 'system.ai.model-b', launched: 'system.ai.model-b',
  }) }));
  on('fs.write', ($, e) => { writes.push(e.path); return { value: undefined }; });
  on('ui.render', ($, e) => {
    renders.push(e);
    return { type: 'Text', props: {}, children: [e.props.text ?? e.props.input?.description ?? 'native row'] };
  });
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/workspace' });
  expect(writes).toEqual([]);
  const notice = { ...panel, component: 'UserMessage' as const, requestId: 'hidden-notice',
    props: { text: 'Agent "Inspect parser" finished', origin: { kind: 'task-notification' as const },
      task: { id: 'child', status: 'completed' } } };
  const notification = await $.ui.mount(notice);
  expect(renders[renders.length - 1].props).toEqual(notice.props);
  await notification.unmount();
  const tool = await $.ui.mount(row('hidden-call'));
  expect(renders[renders.length - 1].props.input).toEqual(originalInput);
  await tool.unmount();
});

test('completed notification labels restore per child and render without storage reads', async ($, on) => {
  const files = new Map<string, string>();
  const writes: any[] = [];
  const renders: any[] = [];
  const forwarded: any[] = [];
  const warnings: string[] = [];
  let idReads = 0, fileReads = 0, environmentReads = 0;
  let sessionId = 'session-1';
  let failRestore = false;
  const directory = '/plugin-data/session-1';
  on('session.id', () => { idReads++; return { value: sessionId }; });
  on('env.get', ($, e) => {
    if (e.name === 'UCODE_AGENT_LABELS_DIR') { environmentReads++; return { value: '/plugin-data' }; }
    return { value: undefined };
  });
  on('env.set', () => ({ value: undefined }));
  on('session.start', ($, e) => ({ cwd: e.cwd }));
  on('session.end', () => ({ sessionId }));
  on('fs.exists', ($, e) => ({ value: [...files.keys()].some(path => path.startsWith(e.path + '/')) }));
  on('fs.list', ($, e) => ({ value: [...files.keys()]
    .filter(path => path.startsWith(e.path + '/'))
    .map(path => ({ name: path.slice(e.path.length + 1), kind: 'file', size: 100, mtimeMs: 0, isLink: false })) }));
  on('fs.read', ($, e) => { fileReads++; return { value: files.get(e.path) }; });
  on('fs.write', ($, e) => {
    if (failRestore && e.path.endsWith('/agent-names/agent-bad.json')) return { deny: 'write denied' };
    writes.push(e); files.set(e.path, e.text); return { value: undefined };
  });
  on('ui.log', ($, e) => { warnings.push(e.text); return { value: undefined }; });
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
    model: 'catalog.schema.selected-model', rationale: 'Selected for this task.',
  }) } }));
  on('tool.call', () => ({ result: 'launched' }));
  on('agent.spawn', ($, e) => {
    forwarded.push(e);
    return { agentId: e.tool_use_id === 'call-1' ? 'child-1' : e.tool_use_id, model: 'catalog.schema.launched-model' };
  });
  on('ui.render', ($, e) => {
    renders.push(e);
    return { type: 'Text', props: {}, children: [e.props.text] };
  });
  const input = { ...originalInput, description: 'Joke from Sonnet' };
  await $.tool.call({ tool: 'Agent', tool_use_id: 'call-1', ...input });
  const spawn = {
    tool_use_id: 'call-1', prompt: input.prompt, description: input.description,
    subagentType: input.subagent_type, parentModel: 'parent-model',
    provider: { plugin: 'engine', tier: 'core' as const }, background: true, fork: false,
    name: 'native-address', permissionMode: 'default', parentAgentId: undefined, cwd: '/workspace',
  };
  await $.agent.spawn(spawn);
  // The spawn carries the routed label; the reviewed call keeps its written description.
  const { description: spawnLabel, ...forwardedSpawn } = forwarded[0];
  const { description: _written, ...spawnRest } = spawn;
  expect(forwardedSpawn).toEqual({ ...spawnRest, model: 'catalog.schema.selected-model' });
  expect(spawnLabel).toMatch(/^[A-Z][a-z]+(?: 2)? · selected-model · inspect the parser$/);
  const key = `${directory}/agent-child-1.json`;
  const saved = JSON.parse(files.get(key)!);
  expect(writes.length).toBe(2);
  expect(writes[0].text).toBe(writes[1].text);
  expect(saved.name).toMatch(/ — launched-model ⚠ selected selected-model$/);
  expect(saved.description).toBe(input.description);
  expect(saved.agent_id).toBe('child-1');
  const notice = {
    ...panel, component: 'UserMessage' as const, requestId: 'notice-1',
    props: { text: 'Agent "Joke from Sonnet" finished', origin: { kind: 'task-notification' as const },
      task: { id: 'child-1', status: 'completed', toolUseId: 'call-1', durationMs: 4200 }, isExpanded: false },
  };
  const { plugin: _plugin, ...expected } = notice;
  const counts = [idReads, fileReads, environmentReads];
  // The notice quotes the routed label; the description's requested model is not printed.
  const flagged = /^Agent "[A-Z][a-z]+(?: 2)? · launched-model · inspect the parser ⚠ selected selected-model" finished$/;
  let quoted = '';
  for (let index = 0; index < 3; index++) {
    const ui = await $.ui.mount(notice);
    const text = renders[renders.length - 1].props.text;
    expect(text).toMatch(flagged);
    expect(await ui.find({ type: 'Text', text })).toBeDefined();
    quoted ||= text;
    expect(renders[renders.length - 1]).toEqual({ ...expected, props: { ...notice.props, text: quoted } });
    await ui.unmount();
  }
  expect([idReads, fileReads, environmentReads]).toEqual(counts);
  expect(notice.props.text).toBe('Agent "Joke from Sonnet" finished');
  for (const [text, suffix] of [
    ['Agent "Joke from Sonnet"\nSummary', '\nSummary'],
    ['Agent "Joke from Sonnet".', '.'],
    ['Agent "Joke from Sonnet"', ''],
    ['Agent "Joke from Sonn" finished', ' finished'],
  ]) {
    const ui = await $.ui.mount({ ...notice, props: { ...notice.props, text } });
    expect(renders[renders.length - 1].props.text).toBe(quoted.replace(/ finished$/, suffix));
    await ui.unmount();
  }
  const unmatched = await $.ui.mount({ ...notice, props: { ...notice.props,
    text: 'Agent "other task" finished\nSummary: "Joke from Sonnet"' } });
  expect(renders[renders.length - 1].props.text).toBe('Agent "other task" finished\nSummary: "Joke from Sonnet"\n⚠ Launched on launched-model, not the selected selected-model');
  await unmatched.unmount();
  // A notice quoting the spawn-time heading matches through the routed label, not the
  // written description, and is rewritten to the receipt's label with one warning.
  const animal = spawnLabel.split(' · ')[0];
  const headed = await $.ui.mount({ ...notice, props: { ...notice.props,
    text: `Agent "${animal} · selected-model · inspect the parser" finished` } });
  expect(renders[renders.length - 1].props.text)
    .toBe(`Agent "${animal} · launched-model · inspect the parser ⚠ selected selected-model" finished`);
  await headed.unmount();
  for (const isExpanded of [false, true]) {
    const ui = await $.ui.mount({ ...notice, props: { ...notice.props, isExpanded,
      text: 'Original task summary\nFull child output stays visible.' } });
    expect(await ui.find({ type: 'Text', text: 'Original task summary\nFull child output stays visible.\n⚠ Launched on launched-model, not the selected selected-model' })).toBeDefined();
    await ui.unmount();
  }
  files.clear();
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/workspace' });
  const resumed = await $.ui.mount(notice);
  expect(renders[renders.length - 1]).toEqual(expected);
  files.set(`${directory}/agent-bad.json`, JSON.stringify({ ...saved, agent_id: 'bad' }));
  files.set(key, JSON.stringify(saved));
  files.set('/plugin-data/another-session/agent-other.json', JSON.stringify({ ...saved, session_id: 'another-session' }));
  failRestore = true;
  const before = writes.length;
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/workspace' });
  expect(writes.length).toBe(before + 1);
  expect(writes[writes.length - 1].path.endsWith('/agent-names/agent-child-1.json')).toBe(true);
  expect(warnings).toEqual(['Could not restore a subagent display label; native labels remain where unavailable.']);
  expect(await resumed.find({ type: 'Text', text: /launched-model · inspect the parser ⚠ selected selected-model/ })).toBeDefined();
  await resumed.unmount();
  failRestore = false;
  for (const value of [null, [], {}, { ...saved, session_id: 'another-session' },
    { ...saved, agent_id: 'wrong-child' }, { ...saved, name: 42 }, { ...saved, description: '' }]) {
    files.clear(); files.set(key, JSON.stringify(value));
    await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/workspace' });
    const invalid = await $.ui.mount(notice);
    expect(renders[renders.length - 1]).toEqual(expected);
    await invalid.unmount();
  }
  for (const [index, description] of ['', ' \t'].entries()) {
    const id = `empty-${index}`;
    await $.tool.call({ tool: 'Agent', tool_use_id: id, ...input, description });
    await $.agent.spawn({ ...spawn, tool_use_id: id, description });
    const actual = JSON.parse(files.get(`${directory}/agent-${id}.json`)!);
    expect(actual.description).toMatch(/ · selected-model · inspect the parser$/);
    // The spawn carries the routed label even when the written description was empty.
    expect(forwarded[forwarded.length - 1].description)
      .toMatch(/^[A-Z][a-z]+(?: 2)? · selected-model · inspect the parser$/);
  }
  for (const props of [
    { ...notice.props, origin: { kind: 'composer' as const }, task: undefined },
    { ...notice.props, task: { ...notice.props.task, id: 'unrouted-child' } },
  ]) {
    const other = await $.ui.mount({ ...notice, props });
    expect(renders[renders.length - 1]).toEqual({ ...expected, props });
    await other.unmount();
  }
  await $.session.end({ reason: 'clear' });
  sessionId = 'session-2';
  await $.tool.call({ tool: 'Agent', tool_use_id: 'after-clear', ...input });
  await $.agent.spawn({ ...spawn, tool_use_id: 'after-clear' });
  const fresh = JSON.parse(files.get('/plugin-data/session-2/agent-after-clear.json')!);
  const cleared = await $.ui.mount({ ...notice, props: { ...notice.props,
    task: { ...notice.props.task, id: 'after-clear' },
  } });
  expect(await cleared.find({ type: 'Text', text: /launched-model · inspect the parser ⚠ selected selected-model/ })).toBeDefined();
  await cleared.unmount();
  // A failed or killed child launched elsewhere is flagged like a completed one.
  const failed = await $.ui.mount({ ...notice, props: { ...notice.props,
    task: { ...notice.props.task, id: 'after-clear', status: 'failed' },
  } });
  expect(renders[renders.length - 1].props.text).toMatch(/^Agent "[A-Z][a-z]+(?: 2)? · launched-model · inspect the parser ⚠ selected selected-model" finished$/);
  await failed.unmount();
  await $.session.end({ reason: 'resume' });
  const retained = await $.ui.mount({ ...notice, props: { ...notice.props,
    task: { ...notice.props.task, id: 'after-clear' },
  } });
  expect(await retained.find({ type: 'Text', text: /launched-model · inspect the parser ⚠ selected selected-model/ })).toBeDefined();
  await retained.unmount();
});

// The stream rewrite needs Claude's local auto-mode fallback check: when it is off (any of
// these set, mirroring the host's own read — any non-empty value for the first two, and
// 1/true/yes/on for DO_NOT_TRACK), the reply passes through untouched and the call routes at
// tool.call instead. A server-side verdict and a hidden model display skip it too.
for (const [show, envName, envValue, rewrites] of [
  ['1', undefined, undefined, true],
  ['0', undefined, undefined, false],
  ['1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', '1', false],
  ['1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', '0', false],
  ['1', 'DISABLE_TELEMETRY', '1', false],
  ['1', 'DO_NOT_TRACK', '1', false],
  ['1', 'DO_NOT_TRACK', '0', true],
  ['1', 'CLAUDE_CODE_AUTO_MODE_SERVER', '1', false],
] as const) {
  test(`parallel streamed calls with model display ${show} (${envName ? `${envName}=${envValue}` : 'local fallback on'})`, {
    options: { ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI: show },
  }, async ($, on) => {
    const routerCalls: any[] = [];
    const spawned: any[] = [];
    const rows = new Map<string, any>();
    on('session.id', () => ({ value: 'session-1' }));
    on('env.get', ($, e) => ({ value: e.name === 'UCODE_AGENT_LABELS_DIR' ? '/plugin-data'
      : e.name === envName ? envValue : undefined }));
    on('fs.write', () => ({ value: undefined }));
    on('process.run', ($, event) => {
      const request = JSON.parse(event.init.stdin);
      routerCalls.push(request);
      return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
        model: request.tool_use_id === 'head-1' ? 'system.ai.model-b' : 'system.ai.glm-5-3-flash', rationale: 'Selected.',
      }) } };
    });
    on('tool.call', () => ({ result: 'launched' }));
    on('agent.spawn', ($, e) => {
      spawned.push(e);
      // The second child launches on a different model than selected.
      return { agentId: `child-${e.tool_use_id}`, model: e.tool_use_id === 'head-1' ? 'system.ai.model-c' : e.model };
    });
    on('ui.render', ($, e) => {
      rows.set(e.requestId, e.props);
      return { type: 'Text', props: {}, children: [e.props.label ?? 'native row'] };
    });
    const calls = ['Sonnet', 'Haiku'].map((model, index) => ({
      prompt: 'Tell me a short joke. Make it dry.', description: `Joke from ${model}`,
      subagent_type: 'general-purpose', run_in_background: true, tag: index,
    }));
    const read = { file_path: 'README.md' };
    on('turn.step', async function* ($, e) {
      yield { kind: 'text', index: 0, text: 'Starting.' };
      yield { kind: 'tool', index: 1, id: 'head-0', name: 'Agent' };
      yield { kind: 'input', index: 1, json: JSON.stringify(calls[0]).slice(0, 20) };
      yield { kind: 'tool', index: 2, id: 'read-0', name: 'Read' };
      yield { kind: 'input', index: 1, json: JSON.stringify(calls[0]).slice(20) };
      yield { kind: 'input', index: 2, json: JSON.stringify(read) };
      yield { kind: 'tool', index: 3, id: 'head-1', name: 'Agent' };
      yield { kind: 'input', index: 3, json: JSON.stringify(calls[1]) };
      yield { kind: 'stop', stopReason: 'tool_use', usage: null };
      return { turnId: e.turnId, index: e.index, answer: 'Starting.', toolUses: [], stopReason: 'tool_use', usage: null };
    });
    const turn = await collectParentTurn($, 'headed', 0);
    const streamedCalls = [1, 3].map(index => JSON.parse(turn.chunks
      .filter(chunk => chunk.kind === 'input' && chunk.index === index).map(chunk => chunk.json).join('')));
    expect(JSON.parse(turn.chunks.find(chunk => chunk.index === 2 && chunk.kind === 'input').json)).toEqual(read);
    if (rewrites) {
      // The reply is held until both calls are routed, then only each description becomes the
      // routed heading; the split input chunk of the first call is coalesced into one.
      expect(turn.chunks.map(chunk => chunk.kind)).toEqual(
        ['text', 'tool', 'input', 'tool', 'input', 'tool', 'input', 'stop']);
      expect(streamedCalls[0].description).toMatch(/^[A-Z][a-z]+(?: 2)? · glm-5-3-flash · Tell me a short joke$/);
      expect(streamedCalls[1].description).toMatch(/^[A-Z][a-z]+(?: 2)? · model-b · Tell me a short joke$/);
      for (const [index, call] of streamedCalls.entries())
        expect(call).toEqual({ ...calls[index], description: call.description });
      expect(routerCalls.map(call => call.tool_input)).toEqual(calls);
    } else {
      // The reply streams untouched, byte-identical, and tool.call does the routing.
      expect(turn.chunks.map(chunk => chunk.kind)).toEqual(
        ['text', 'tool', 'input', 'tool', 'input', 'input', 'tool', 'input', 'stop']);
      expect(streamedCalls).toEqual(calls);
      expect(turn.chunks.filter(chunk => chunk.kind === 'input' && chunk.index === 1).map(chunk => chunk.json))
        .toEqual([JSON.stringify(calls[0]).slice(0, 20), JSON.stringify(calls[0]).slice(20)]);
      expect(routerCalls).toHaveLength(0);
    }
    await Promise.all(streamedCalls.map((call, index) =>
      $.tool.call({ tool: 'Agent', tool_use_id: `head-${index}`, ...call })));
    // Exactly one routing decision per call, whichever path made it.
    expect(routerCalls.map(call => call.tool_input)).toEqual(calls);
    for (const [index, call] of streamedCalls.entries()) {
      await $.agent.spawn({
        tool_use_id: `head-${index}`, prompt: calls[index].prompt, description: call.description,
        subagentType: 'general-purpose', parentModel: 'parent', provider: { plugin: 'engine', tier: 'core' as const },
        background: true, fork: false, permissionMode: 'default', cwd: '/workspace',
      });
    }
    expect(spawned.map(event => event.model)).toEqual(['system.ai.glm-5-3-flash', 'system.ai.model-b']);
    // The task label is set at spawn on either side of the gate, whatever the stream did;
    // a hidden model display leaves the recorded description in place.
    if (show === '1') for (const event of spawned) expect(event.description).toMatch(new RegExp(
      `^[A-Z][a-z]+(?: 2)? · ${event.tool_use_id === 'head-1' ? 'model-b' : 'glm-5-3-flash'} · Tell me a short joke$`));
    else for (const event of spawned) expect(event.description).toBe(
      calls[Number(event.tool_use_id === 'head-1')].description);
    // Claude before 2.1.328 has no agent-list row to render.
    const agentRow = async (id: string) => {
      try {
        return await $.ui.mount({ ...panel, component: 'AgentRow' as const, requestId: id, props: { id } });
      } catch (error) {
        if (String(error).includes('is not a component')) return undefined;
        throw error;
      }
    };
    const match = await agentRow('child-head-0');
    if (match) {
      if (show === '1') expect(rows.get('child-head-0').label).toMatch(/^[A-Z][a-z]+ · glm-5-3-flash · Tell me a short joke$/);
      else expect(rows.get('child-head-0').label).toBeUndefined();
      await match.unmount();
      const mismatch = await agentRow('child-head-1');
      if (show === '1') expect(rows.get('child-head-1').label).toMatch(/ · model-c · Tell me a short joke ⚠ selected model-b$/);
      else expect(rows.get('child-head-1').label).toBeUndefined();
      await mismatch!.unmount();
    }
    // The notice for the child that launched elsewhere says so; the matching one is left alone.
    const notice = (id: string) => ({
      ...panel, component: 'UserMessage' as const, requestId: `notice-${id}`,
      props: { text: 'Agent finished', origin: { kind: 'task-notification' as const },
        task: { id, status: 'completed', toolUseId: id, durationMs: 1 }, isExpanded: false },
    });
    const flagged = await $.ui.mount(notice('child-head-1'));
    expect(rows.get('notice-child-head-1').text).toBe(show === '1'
      ? 'Agent finished\n⚠ Launched on model-c, not the selected model-b' : 'Agent finished');
    await flagged.unmount();
    const clean = await $.ui.mount(notice('child-head-0'));
    expect(rows.get('notice-child-head-0').text).toBe('Agent finished');
    await clean.unmount();
  });

}

test('a failed routing attempt is not repeated for the same tool call', async ($, on) => {
  const routerCalls: any[] = [];
  const call = { prompt: 'Tell me a short joke.', description: 'Joke from Sonnet', subagent_type: 'general-purpose' };
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', ($, event) => { routerCalls.push(event); return { value: { exitCode: 1, stderr: '', stdout: '' } }; });
  on('tool.call', () => ({ result: 'launched' }));
  on('turn.step', async function* ($, e) {
    yield { kind: 'tool', index: 0, id: 'retry-0', name: 'Agent' };
    yield { kind: 'input', index: 0, json: JSON.stringify(call) };
    yield { kind: 'stop', stopReason: 'tool_use', usage: null };
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'tool_use', usage: null };
  });
  const turn = await collectParentTurn($, 'retry', 0);
  expect(JSON.parse(turn.chunks[1].json)).toEqual(call);
  // No env.get handler, so the fallback gate cannot even be read: the reply streams untouched.
  expect(routerCalls).toHaveLength(0);
  await $.tool.call({ tool: 'Agent', tool_use_id: 'retry-0', ...call });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'retry-0', ...call });
  expect(routerCalls).toHaveLength(1);
});

test('a routing error keeps the reply, and a denied spawn that names an agent saves no label', async ($, on) => {
  const writes: any[] = [];
  const logs: string[] = [];
  const call = { prompt: 'Tell me a short joke.', description: 'Joke from Sonnet', subagent_type: 'general-purpose' };
  on('session.id', () => { throw new Error('session unavailable'); });
  on('env.get', ($, e) => ({ value: e.name === 'UCODE_AGENT_LABELS_DIR' ? '/plugin-data' : undefined }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ model: 'system.ai.model-b', rationale: 'r' }) } }));
  on('fs.write', ($, e) => { writes.push(e); return { value: undefined }; });
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
  on('tool.call', () => ({ result: 'launched' }));
  on('turn.step', async function* ($, e) {
    yield { kind: 'tool', index: 0, id: 'err-0', name: 'Agent' };
    yield { kind: 'input', index: 0, json: JSON.stringify(call) };
    yield { kind: 'stop', stopReason: 'tool_use', usage: null };
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'tool_use', usage: null };
  });
  const turn = await collectParentTurn($, 'route-error', 0);
  expect(turn.chunks.map(chunk => chunk.kind)).toEqual(['tool', 'input', 'stop']);
  expect(JSON.parse(turn.chunks[1].json)).toEqual(call);
  await $.tool.call({ tool: 'Agent', tool_use_id: 'err-0', ...call });
  expect(logs).toContain('Smart Router could not route this subagent; using its original settings.');
  expect(writes).toHaveLength(0);
});

test('a denied spawn that still names an agent saves no display label', async ($, on) => {
  const writes: any[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('env.get', ($, e) => ({ value: e.name === 'UCODE_AGENT_LABELS_DIR' ? '/plugin-data' : undefined }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ model: 'system.ai.model-b', rationale: 'r' }) } }));
  on('tool.call', () => ({ result: 'launched' }));
  on('agent.spawn', () => ({ deny: 'refused', agentId: 'child-denied' } as any));
  on('fs.write', ($, e) => { writes.push(e); return { value: undefined }; });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'denied-0', ...originalInput });
  await $.agent.spawn({
    tool_use_id: 'denied-0', prompt: originalInput.prompt, description: originalInput.description,
    subagentType: originalInput.subagent_type, parentModel: 'parent', provider: { plugin: 'engine', tier: 'core' as const },
    background: true, fork: false, permissionMode: 'default', cwd: '/workspace',
  });
  expect(writes).toHaveLength(0);
});

test('session start without a display directory restores nothing and logs nothing', async ($, on) => {
  const logs: string[] = [];
  on('session.id', () => ({ value: 'session-1' }));
  on('env.get', () => ({ value: undefined }));
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
  on('session.start', ($, e) => ({ cwd: e.cwd }));
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/workspace' });
  expect(logs).toEqual([]);
});

test('a consumer that stops reading closes the reply stream beneath', async ($, on) => {
  let closed = false;
  const call = { prompt: 'Tell me a short joke.', description: 'Joke from Sonnet', subagent_type: 'general-purpose' };
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ model: 'system.ai.model-b', rationale: 'r' }) } }));
  on('turn.step', async function* ($, e) {
    try {
      yield { kind: 'tool', index: 0, id: 'close-0', name: 'Agent' };
      yield { kind: 'input', index: 0, json: JSON.stringify(call) };
      yield { kind: 'stop', stopReason: 'tool_use', usage: null };
    } finally {
      closed = true;
    }
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'tool_use', usage: null };
  });
  const stream = $.turn.step({ turnId: 'close', index: 0, model: 'parent-model', messageCount: 3, agentId: undefined });
  await stream.next();
  await stream.return(undefined);
  expect(closed).toBe(true);
});
