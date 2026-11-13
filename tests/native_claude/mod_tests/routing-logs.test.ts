import { expect, test } from 'claude-code/testing';

const options = { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', logsCommand: ['/bin/ug', 'smart-router-logs'] };
const pane = {
  plugin: 'ug-smart-router', component: 'Pane', requestId: 'smart-router-logs', surface: 'terminal',
  props: { title: 'Smart Router logs', isFocused: true, bodyColumns: 40, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const;
const command = { command: 'smart-router', args: 'logs' };

test('renders session rows and refreshes on new subagent spawns only while open', { options }, async ($, on) => {
  const spawn = {
    tool_use_id: 'call', prompt: 'Inspect parser', description: 'Inspect parser',
    subagentType: 'general-purpose', parentModel: 'claude-sonnet-5',
    provider: { plugin: 'engine', tier: 'core' as const }, background: true, fork: false,
    permissionMode: 'default', parentAgentId: 'parent', cwd: '/workspace',
  };
  const now = Date.now() / 1000;
  let decisions: unknown[] = [
    { at: now - 2 * 3600, preview: 'older task ' + 'A'.repeat(200), selected_model: 'system.ai.claude-sonnet', routing_ms: 1400 },
    { at: now - 5, preview: null, selected_model: 'databricks-fast', routing_ms: 250 },
    { at: 'invalid', preview: null, selected_model: 'bad-row', routing_ms: 0 },
  ];
  let reads = 0;
  let pause = false;
  let resume: () => void = () => {};
  const reader = new Promise<void>((resolve) => { resume = resolve; });
  let open = [{ id: pane.requestId }];
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', ($, e) => {
    expect(e).toMatchObject({ id: pane.requestId, focus: true, closeOnEscape: true });
    return { value: { isPlaced: true } };
  });
  on('ui.panes', () => ({ value: open }));
  on('agent.spawn', ($, e) => e.name === 'denied' ? { deny: 'Permission denied' } : { agentId: 'child', model: 'claude-sonnet-5' });
  on('turn.complete', () => ({ text: '' }));
  on('process.run', async ($, e) => {
    expect(e.argv).toEqual(['/bin/ug', 'smart-router-logs', '--session', 'session-one']);
    reads++;
    if (pause) await reader;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({ v: 1, decisions }) } };
  });
  await $.turn.complete({ answer: '' });
  await $.agent.spawn(spawn);
  expect(reads).toBe(0);
  expect((await $.command.run(command)).text).toBe('Smart Router logs opened.');
  const ui = await $.ui.mount(pane);
  const tree = JSON.stringify(await ui.drawn());
  expect(tree).toContain('2 routed prompts this session');
  expect(tree).toContain('2h ago');
  expect(tree).toContain('just now');
  expect(tree).toContain('claude-sonnet');
  expect(tree).toContain('fast');
  expect(tree).not.toContain('bad-row');
  expect(tree).toContain('…');
  expect(tree).not.toContain('A'.repeat(40));
  expect(tree).toContain('(prompt not recorded)');
  expect(tree.indexOf('(prompt not recorded)')).toBeLessThan(tree.indexOf('older task'));
  await $.turn.complete({ answer: '' });
  await $.agent.spawn({ ...spawn, resume: { agentId: 'child' } });
  await $.agent.spawn({ ...spawn, name: 'denied' });
  expect(reads).toBe(1);
  decisions = [];
  pause = true;
  await $.agent.spawn(spawn);
  await $.agent.spawn({ ...spawn, tool_use_id: 'parallel', background: false });
  expect(reads).toBe(2);
  expect(JSON.stringify(await ui.drawn())).toContain('older task');
  pause = false;
  resume();
  expect(JSON.stringify(await ui.drawn())).toContain('No routed subagents in this session yet.');
  await $.command.run(command);
  expect(reads).toBe(4);
  open = [];
  await $.agent.spawn(spawn);
  await $.agent.spawn(spawn);
  await $.turn.complete({ answer: '' });
  expect(reads).toBe(4);
  await ui.unmount();
});

test('shows reader failures, clears stale errors, and reports an unplaced pane', { options }, async ($, on) => {
  let stdout = '';
  let exitCode = 0;
  let isStdoutTruncated = false;
  let isPlaced = true;
  let closed = false;
  on('session.id', () => ({ value: 'session-one' }));
  on('ui.open', () => ({ value: { isPlaced } }));
  on('ui.close', () => { closed = true; return { value: undefined }; });
  on('process.run', () => ({ value: { exitCode, stderr: '', stdout, isStdoutTruncated } }));
  const ui = await $.ui.mount(pane);
  for (const response of [
    { exitCode: 1, stdout: JSON.stringify({ v: 1, error: 'Permission denied' }), hint: true, isStdoutTruncated: false },
    { exitCode: 1, stdout: '', hint: false, isStdoutTruncated: false },
    { exitCode: 0, stdout: 'invalid json', hint: false, isStdoutTruncated: false },
    { exitCode: 0, stdout: '{"v":2,"decisions":[]}', hint: false, isStdoutTruncated: false },
    { exitCode: 0, stdout: '{"v":1,"decisions":[]}', hint: false, isStdoutTruncated: true },
  ]) {
    ({ exitCode, stdout, isStdoutTruncated } = response);
    await $.command.run(command);
    const tree = JSON.stringify(await ui.drawn());
    expect(tree).toContain('Could not read Smart Router logs.');
    expect(tree.includes('Permission denied')).toBe(response.hint);
  }
  stdout = '{"v":1,"decisions":[]}';
  isStdoutTruncated = false;
  await $.command.run(command);
  expect(JSON.stringify(await ui.drawn())).toContain('No routed subagents in this session yet.');
  isPlaced = false;
  expect((await $.command.run(command)).text).toBe('Could not open the Smart Router logs pane.');
  expect(closed).toBe(true);
  await ui.unmount();
});

test('missing reader configuration shows an error', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1' } }, async ($, on) => {
  on('ui.open', () => ({ value: { isPlaced: true } }));
  await $.command.run(command);
  const ui = await $.ui.mount(pane);
  expect(JSON.stringify(await ui.drawn())).toContain('Could not read Smart Router logs.');
  await ui.unmount();
});

test('unrouted launch declines logs and usage includes logs', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0' } }, async ($) => {
  expect((await $.command.run(command)).text).toContain('was not enabled');
  expect((await $.command.run({ command: 'smart-router', args: 'wrong' })).text).toBe('Usage: /smart-router on|off|logs|explain on|off|latest');
});
