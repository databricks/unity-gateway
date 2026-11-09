import { expect, test } from 'claude-code/testing';

const options = { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', logsCommand: ['/bin/ug', 'smart-router-logs'] };
const pane = {
  plugin: 'ug-smart-router', component: 'Pane', requestId: 'smart-router-logs', surface: 'terminal',
  props: { title: 'Smart Router logs', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const;
const command = { command: 'smart-router', args: 'logs' };
const records = [
  { at: 1000, preview: 'older task', selected_model: 'system.ai.claude-sonnet', routing_ms: 1400 },
  { at: 2000, preview: null, selected_model: 'databricks-fast', routing_ms: 250 },
];

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
  expect(tree).toMatch(/\d{2}:\d{2}/);
  expect(tree.indexOf('(prompt not recorded)')).toBeGreaterThanOrEqual(0);
  expect(tree.indexOf('older task')).toBeGreaterThanOrEqual(0);
  expect(tree.indexOf('(prompt not recorded)')).toBeLessThan(tree.indexOf('older task'));
  expect(tree).toContain('claude-sonnet');
  expect(tree).toContain('fast');
  expect(tree).toContain('1.4s');
  expect(tree).toContain('0.3s');
  expect(tree).toContain('green');
  await $.command.run(command);
  expect(JSON.stringify(await ui.drawn())).toContain('No routed subagents in this session yet.');
  expect(count).toBe(2);
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
  expect(fallbackTree).not.toContain('A'.repeat(60));
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

test('unrouted launch declines logs and usage includes logs', { options: { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0' } }, async ($) => {
  expect((await $.command.run(command)).text).toContain('was not enabled');
  expect((await $.command.run({ command: 'smart-router', args: 'wrong' })).text).toBe('Usage: /smart-router on|off|logs');
});
