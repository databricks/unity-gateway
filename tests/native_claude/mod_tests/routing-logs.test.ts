import { expect, test } from 'claude-code/testing';

const options = { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', logsCommand: ['/bin/ug', 'smart-router-logs'] };
const pane = {
  plugin: 'ug-smart-router', component: 'Pane', requestId: 'smart-router-logs', surface: 'terminal',
  props: { title: 'Smart Router logs', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const;
const command = { command: 'smart-router', args: 'logs' };
const records = [
  { decision_id: 'd1', at: 1000, preview: 'older task', selected_model: 'system.ai.claude-sonnet', routing_ms: 1400 },
  { decision_id: 'd2', at: 2000, preview: null, selected_model: 'databricks-fast', routing_ms: 250 },
];
const decision = {
  v: 1,
  decision: {
    v: 1, type: 'decision', decision_id: 'd1', at: 1000, harness: 'claude', kind: 'subagent',
    session_id: 'session-one', tool_use_id: 't1',
    prompt: { preview: 'older task', text: 'Full prompt text', sha256: 'abc', length: 16 },
    original: { model: null, subagent_type: 'Explore' },
    selected_model: 'system.ai.claude-sonnet', requested_model: 'system.ai.claude-opus',
    candidates: [
      { model: 'system.ai.claude-sonnet', harness: 'claude' },
      { model: 'system.ai.glm-5', harness: 'claude' },
    ],
    router_name: 'router', routing_ms: 840, rationale: 'Because it is a search.', raw_rationale: null,
  },
};

test('opens and refreshes session rows newest first', { options }, async ($, on) => {
  const opened: unknown[] = [];
  let count = 0;
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', ($, e) => { opened.push(e); return { value: { isPlaced: true } }; });
  on('process.run', ($, e) => {
    expect(e.argv).toEqual(['/bin/ug', 'smart-router-logs', '--session', 'session-one', '--harness', 'claude', '--json']);
    count++;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: count === 1 ? records : [] }) } };
  });
  expect((await $.command.run(command)).text).toBe('Smart Router logs opened.');
  expect(opened[0]).toMatchObject({ id: pane.requestId, title: pane.props.title, focus: true, closeOnEscape: true });
  const ui = await $.ui.mount(pane);
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('2 routed prompts this session');
  expect(tree).not.toMatch(/\d{2}:\d{2}/);
  expect(tree.indexOf('(prompt not recorded)')).toBeLessThan(tree.indexOf('older task'));
  expect(tree).toContain('claude-sonnet');
  expect(tree).toContain('fast');
  expect(tree).not.toContain('1.4s');
  expect(tree).not.toContain('0.3s');
  expect(tree).toContain('green');
  await $.command.run(command);
  expect(JSON.stringify(await ui.drawn())).toContain('No routed subagents in this session yet.');
  expect(count).toBe(2);
  await ui.unmount();
});

test('pressing a row fetches the decision and Back returns to the list', { options }, async ($, on) => {
  const runs: string[][] = [];
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('process.run', ($, e) => {
    runs.push(e.argv);
    if (e.argv.includes('--decision')) return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify(decision) } };
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: records }) } };
  });
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  await ui.press({ key: 'd1' });
  expect(runs[1]).toEqual(['/bin/ug', 'smart-router-logs', '--session', 'session-one', '--harness', 'claude', '--decision', 'd1', '--json']);
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('Full prompt text');
  expect(tree).toContain('Prompt (Explore)');
  expect(tree).toContain('claude-sonnet');
  expect(tree).toContain('0.8s');
  expect(tree).toContain('Requested: claude-opus');
  expect(tree).toContain('Because it is a search.');
  expect(tree).toContain('● claude-sonnet');
  expect(tree).toContain('○ glm-5');
  await ui.press({ key: 'back' });
  expect(JSON.stringify(await ui.drawn())).toContain('2 routed prompts this session');
  await ui.press({ key: 'd1' });
  await $.command.run(command);
  expect(JSON.stringify(await ui.drawn())).toContain('2 routed prompts this session');
  await ui.unmount();
});

test('hides the requested model when it matches the selection', { options }, async ($, on) => {
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('process.run', ($, e) => {
    if (e.argv.includes('--decision')) {
      return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
        ...decision,
        decision: { ...decision.decision, requested_model: 'system.ai.claude-sonnet', rationale: null, prompt: { ...decision.decision.prompt, text: null } },
      }) } };
    }
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: records }) } };
  });
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  await ui.press({ key: 'd1' });
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).not.toContain('Requested:');
  expect(tree).toContain('(prompt not recorded)');
  expect(tree).toContain('No rationale returned.');
  await ui.unmount();
});

test('a decision that fails to load shows an error with a way back', { options }, async ($, on) => {
  const logs: string[] = [];
  let decisionStdout = '{"v":1,"error":"decision not found"}';
  let decisionExit = 1;
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
  on('process.run', ($, e) => {
    if (e.argv.includes('--decision')) return { value: { exitCode: decisionExit, stderr: '', stdout: decisionStdout } };
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: records }) } };
  });
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  for (const [stdout, exitCode] of [['{"v":1,"error":"decision not found"}', 1], ['not json', 0], ['{"v":1,"decision":{"v":1}}', 0]] as const) {
    decisionStdout = stdout;
    decisionExit = exitCode;
    await ui.press({ key: 'd1' });
    expect(JSON.stringify(await ui.drawn())).toContain('Could not load this routing decision.');
    await ui.press({ key: 'back' });
    expect(JSON.stringify(await ui.drawn())).toContain('2 routed prompts this session');
  }
  expect(logs).toHaveLength(3);
  await ui.unmount();
});

test('truncates long previews for the pane width', { options }, async ($, on) => {
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
    v: 1, decisions: [{ ...records[0], preview: 'A'.repeat(200) }],
  }) } }));
  await $.command.run(command);
  const ui = await $.ui.mount({ ...pane, props: { ...pane.props, bodyColumns: 40 } });
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('…');
  expect(tree).not.toContain('A'.repeat(40));
  await ui.unmount();
});

test('shows a failure for command errors and malformed JSON', { options }, async ($, on) => {
  let stdout = 'invalid json';
  let exitCode = 0;
  const logs: string[] = [];
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
  on('process.run', () => ({ value: { exitCode, stderr: '', stdout } }));
  const ui = await $.ui.mount(pane);
  for (const value of ['invalid json', '{"v":1,"decisions":[{}]}', '{"v":1,"decisions":[]}']) {
    stdout = value;
    if (value.includes('[]')) exitCode = 1;
    await $.command.run(command);
    expect(JSON.stringify(await ui.drawn())).toContain('Could not read Smart Router logs.');
  }
  expect(logs).toHaveLength(3);
  await ui.unmount();
});

test('missing command fails safely and unrouted launches stay inert', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
  let logged = 0;
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('ui.log', () => { logged++; return { value: undefined }; });
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  expect(JSON.stringify(await ui.drawn())).toContain('Could not read Smart Router logs.');
  expect(logged).toBe(1);
  await ui.unmount();
});

test('unrouted launch declines logs and usage includes logs', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0' } }, async ($) => {
  expect((await $.command.run(command)).text).toContain('was not enabled');
  expect((await $.command.run({ command: 'smart-router', args: 'wrong' })).text).toBe('Usage: /smart-router on|off|logs');
});
