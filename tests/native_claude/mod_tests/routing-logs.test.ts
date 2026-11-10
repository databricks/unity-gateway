import { expect, test } from 'claude-code/testing';

const options = { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', logsCommand: ['/bin/ug', 'smart-router-logs'] };
const pane = {
  plugin: 'ug-smart-router', component: 'Pane', requestId: 'smart-router-logs', surface: 'terminal',
  props: { title: 'Smart Router logs', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const;
const command = { command: 'smart-router', args: 'logs' };
const now = Date.now() / 1000;
const records = [
  { decision_id: 'd1', at: now - 2 * 3600, preview: 'older task', selected_model: 'system.ai.claude-sonnet', routing_ms: 1400 },
  { decision_id: 'd2', at: now - 5, preview: null, selected_model: 'databricks-fast', routing_ms: 250 },
];
const decision = {
  v: 1,
  decision: {
    v: 1, type: 'decision', decision_id: 'd1', at: 1000, harness: 'claude', kind: 'subagent',
    session_id: 'session-one', tool_use_id: 't1',
    prompt: { preview: 'Recorded prompt preview', sha256: 'a'.repeat(64), length: 23 },
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
    expect(e.argv).toEqual(['/bin/ug', 'smart-router-logs', '--session', 'session-one']);
    count++;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: count === 1 ? records : [] }) } };
  });
  expect((await $.command.run(command)).text).toBe('Smart Router logs opened.');
  expect(opened[0]).toMatchObject({ id: pane.requestId, title: pane.props.title, focus: true, closeOnEscape: true });
  const ui = await $.ui.mount(pane);
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('2 routed prompts this session');
  expect(tree).toContain('  2h ago  ');
  expect(tree).toContain('just now  ');
  expect(tree).not.toMatch(/\d{2}:\d{2}/);
  expect(tree.indexOf('(prompt not recorded)')).toBeGreaterThanOrEqual(0);
  expect(tree.indexOf('older task')).toBeGreaterThanOrEqual(0);
  expect(tree.indexOf('(prompt not recorded)')).toBeLessThan(tree.indexOf('older task'));
  expect(tree).toContain('claude-sonnet');
  expect(tree).toContain('fast');
  expect(tree).not.toContain('1.4s');
  expect(tree).not.toContain('0.3s');
  // Rows are pressable Buttons labeled with the age, the preview, and the short model.
  expect(tree).toContain('"type":"Button"');
  // Each row fills bodyColumns exactly, so the model name is right-aligned.
  expect(tree).toContain(`"key":"d2","label":"just now  ${'(prompt not recorded)'.padEnd(80 - 'fast'.length - 12)}  fast"`);
  expect(tree).toContain(`"key":"d1","label":"  2h ago  ${'older task'.padEnd(80 - 'claude-sonnet'.length - 12)}  claude-sonnet"`);
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
  expect(runs[1]).toEqual(['/bin/ug', 'smart-router-logs', '--session', 'session-one', '--decision', 'd1']);
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('Recorded prompt preview');
  // Labels are dim and sit in one fixed-width column ('Prompt preview: ' is the
  // longest label; the width is its length plus a one-space gutter) that never
  // shrinks, and each value fills the rest of the row so values align.
  expect(tree).toContain('"props":{"dimColor":true},"children":["Prompt preview: "]');
  expect(tree).toContain('"props":{"dimColor":true},"children":["Agent type: "]');
  expect(tree).toContain('"props":{"width":17,"flexShrink":0}');
  expect(tree).toContain('"props":{"flexGrow":1}');
  expect(tree.indexOf('Prompt preview: ')).toBeLessThan(tree.indexOf('Agent type: '));
  expect(tree).not.toContain('SHA-256');
  expect(tree).not.toContain('Full prompt is not stored.');
  // The routing outcome is three labeled lines in order.
  expect(tree.indexOf('Selected: ')).toBeGreaterThanOrEqual(0);
  expect(tree.indexOf('Agent type: ')).toBeLessThan(tree.indexOf('Selected: '));
  expect(tree.indexOf('Selected: ')).toBeLessThan(tree.indexOf('Routing Time: '));
  expect(tree.indexOf('0.8s')).toBeLessThan(tree.indexOf('Rationale: '));
  expect(tree.indexOf('Rationale: ')).toBeLessThan(tree.indexOf('Because it is a search.'));
  expect(tree).toContain('"props":{"dimColor":true},"children":["Selected: "]');
  expect(tree).toContain('"props":{"dimColor":true},"children":["Routing Time: "]');
  expect(tree).toContain('"props":{"dimColor":true},"children":["Rationale: "]');
  // The record's requested model differs from the selection, yet no Requested line is shown.
  expect(tree).not.toContain('Requested:');
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

test('ignores a late decision load after a newer selection', { options }, async ($, on) => {
  let releaseOld: (() => void) | undefined;
  const oldResult = new Promise(resolve => {
    releaseOld = () => resolve({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify(decision) } });
  });
  const newestDecision = {
    ...decision,
    decision: { ...decision.decision, decision_id: 'd2', prompt: { preview: 'newer decision', sha256: 'b'.repeat(64), length: 14 } },
  };
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('process.run', ($, e) => {
    if (e.argv.includes('--decision')) {
      if (e.argv.includes('d1')) return oldResult;
      return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify(newestDecision) } };
    }
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: records }) } };
  });
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  const oldLoad = ui.press({ key: 'd1' });
  await ui.press({ key: 'd2' });
  expect(JSON.stringify(await ui.drawn())).toContain('newer decision');
  releaseOld?.();
  await oldLoad;
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('newer decision');
  expect(tree).not.toContain('Recorded prompt preview');
  await ui.unmount();
});

test('ignores a late decision load after rerunning logs', { options }, async ($, on) => {
  let releaseOld: (() => void) | undefined;
  let signalStarted: (() => void) | undefined;
  const started = new Promise<void>(resolve => { signalStarted = resolve; });
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('process.run', ($, e) => {
    if (e.argv.includes('--decision')) {
      return new Promise(resolve => {
        releaseOld = () => resolve({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify(decision) } });
        signalStarted?.();
      });
    }
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: records }) } };
  });
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  const oldLoad = ui.press({ key: 'd1' });
  await started;
  await $.command.run(command);
  releaseOld?.();
  await oldLoad;
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('2 routed prompts this session');
  expect(tree).not.toContain('Recorded prompt preview');
  await ui.unmount();
});

test('never shows the requested model even when it differs from the selection', { options }, async ($, on) => {
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('process.run', ($, e) => {
    if (e.argv.includes('--decision')) {
      return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
        ...decision,
        decision: { ...decision.decision, rationale: '  ', prompt: { ...decision.decision.prompt, preview: null } },
      }) } };
    }
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: records }) } };
  });
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  await ui.press({ key: 'd1' });
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).not.toContain('Requested:');
  expect(tree).not.toContain('claude-opus');
  expect(tree).toContain('(prompt not recorded)');
  expect(tree).toContain('"props":{"dimColor":true},"children":["Rationale: "]');
  expect(tree).toContain('No rationale returned.');
  await ui.unmount();
});

test('a decision that fails to load shows an error with a way back', { options }, async ($, on) => {
  const logs: string[] = [];
  let decisionStdout = '{"v":1,"error":"decision not found"}';
  let decisionExit = 1;
  let decisionTruncated = false;
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
  on('process.run', ($, e) => {
    if (e.argv.includes('--decision')) {
      return { value: { exitCode: decisionExit, stderr: '', stdout: decisionStdout, isStdoutTruncated: decisionTruncated } };
    }
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: records }) } };
  });
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  const invalidId = JSON.stringify({ ...decision, decision: { ...decision.decision, decision_id: 'different-id' } });
  const failures = [
    ['{"v":1,"error":"decision not found"}', 1, false],
    ['not json', 0, false],
    ['{"v":1,"decision":{"v":1}}', 0, false],
    [invalidId, 0, false],
    [JSON.stringify(decision), 0, true],
  ] as const;
  for (const [index, [stdout, exitCode, truncated]] of failures.entries()) {
    decisionStdout = stdout;
    decisionExit = exitCode;
    decisionTruncated = truncated;
    await ui.press({ key: 'd1' });
    const failedTree = JSON.stringify(await ui.drawn());
    expect(failedTree).toContain('Could not load this routing decision.');
    // The reader CLI's own error envelope is surfaced under the generic notice.
    expect(failedTree.includes('decision not found')).toBe(index === 0);
    await ui.press({ key: 'back' });
    expect(JSON.stringify(await ui.drawn())).toContain('2 routed prompts this session');
  }
  expect(logs).toHaveLength(5);
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
  const { bodyColumns: _omitted, ...fallbackProps } = pane.props;
  const fallback = await $.ui.mount({ ...pane, props: fallbackProps });
  const fallbackTree = JSON.stringify(await fallback.drawn());
  expect(fallbackTree).toContain('…');
  expect(fallbackTree).not.toContain('A'.repeat(63));
  await fallback.unmount();
});

test('shows a failure for command errors and malformed JSON', { options }, async ($, on) => {
  let stdout = 'invalid json';
  const logs: string[] = [];
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout } }));
  const ui = await $.ui.mount(pane);
  for (const value of ['invalid json', '{"v":2,"decisions":[]}', '123']) {
    stdout = value;
    await $.command.run(command);
    expect(JSON.stringify(await ui.drawn())).toContain('Could not read Smart Router logs.');
  }
  expect(logs).toHaveLength(3);
  await ui.unmount();
});

test('drops malformed decision rows without failing the pane', { options }, async ($, on) => {
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
    v: 1, decisions: [{ at: 'nope', preview: null, selected_model: 'x', routing_ms: 0 }, records[1]],
  }) } }));
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('1 routed prompt this session');
  expect(tree).toContain('fast');
  await ui.unmount();
});

test('reports an unplaced pane', { options }, async ($, on) => {
  let closed = 0;
  let logged = 0;
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: false } }));
  on('ui.close', () => { closed++; return { value: undefined }; });
  on('ui.log', () => { logged++; return { value: undefined }; });
  on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions: records }) } }));
  expect((await $.command.run(command)).text).toBe('Could not open the Smart Router logs pane.');
  expect(closed).toBe(1);
  expect(logged).toBe(1);
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

test('a failed read surfaces the CLI error envelope', { options }, async ($, on) => {
  let stdout: string = JSON.stringify({ v: 1, error: 'cannot read session log at /tmp/missing.jsonl' });
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('process.run', () => ({ value: { exitCode: 1, stderr: '', stdout } }));
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('Could not read Smart Router logs.');
  expect(tree).toContain('cannot read session log at /tmp/missing.jsonl');
  await ui.unmount();
  // A failure without a readable envelope keeps only the generic line.
  stdout = '';
  await $.command.run(command);
  const generic = await $.ui.mount(pane);
  const bare = JSON.stringify(await generic.drawn());
  expect(bare).toContain('Could not read Smart Router logs.');
  expect(bare).not.toContain('cannot read session log');
  await generic.unmount();
});

test('the open pane refreshes on turn completion and a closed one stops', { options }, async ($, on) => {
  let reads = 0;
  let decisions: unknown[] = records;
  let open: unknown[] = [{ id: 'smart-router-logs' }];
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('ui.panes', () => ({ value: open }));
  on('turn.complete', () => ({ text: '' }));
  on('process.run', () => {
    reads++;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions }) } };
  });
  // No reader runs while the pane has never been opened.
  await $.turn.complete({ answer: '' });
  expect(reads).toBe(0);
  await $.command.run(command);
  expect(reads).toBe(1);
  const ui = await $.ui.mount(pane);
  expect(JSON.stringify(await ui.drawn())).toContain('older task');
  await ui.unmount();
  // A decision recorded after the pane opened shows up on the next turn completion.
  decisions = [records[1]];
  await $.turn.complete({ answer: '' });
  expect(reads).toBe(2);
  const refreshed = await $.ui.mount(pane);
  const tree = JSON.stringify(await refreshed.drawn());
  expect(tree).toContain('1 routed prompt this session');
  expect(tree).not.toContain('older task');
  await refreshed.unmount();
  // Once the user closes the pane, later turns spawn no reader.
  open = [];
  decisions = records;
  await $.turn.complete({ answer: '' });
  await $.turn.complete({ answer: '' });
  expect(reads).toBe(2);
});

test('unrouted launch declines logs and usage includes logs', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0' } }, async ($) => {
  expect((await $.command.run(command)).text).toContain('was not enabled');
  expect((await $.command.run({ command: 'smart-router', args: 'wrong' })).text).toBe('Usage: /smart-router on|off|logs');
});

test('a UG route agent type is not shown as the prompt label', { options }, async ($, on) => {
  const routed = JSON.stringify({ ...decision, decision: { ...decision.decision,
    original: { model: null, subagent_type: 'ug-smart-router:ucode-route-claude-haiku-5-5-009ef3ad' } } });
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced: true } }));
  on('process.run', ($, e) => ({ value: { exitCode: 0, stderr: '',
    stdout: e.argv.includes('--decision') ? routed : JSON.stringify({ v: 1, decisions: records }) } }));
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  await ui.press({ key: 'd1' });
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('Prompt preview');
  expect(tree).not.toContain('Agent type: ');
  expect(tree).not.toContain('ucode-route-');
  await ui.unmount();
});
