import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';

const id = 'e6ef90cf-25b3-4fee-a356-5f8fbc652e7c';
const command = '/path/to/isaac feedback --category other --background -- "test feedback"';
const receipt = { feedback_id: id, delivery_status: 'queued', evidence_status: 'not_requested' };

// Model the CLI and UI at the native event boundary; the packaged mod handles the real events.
function setup(on: On, enabled = '1') {
  const stdout = `CLI diagnostic\n${JSON.stringify(receipt)}`;
  const answer = { result: { stdout, stderr: '', interrupted: false }, text: stdout };
  const logs: string[] = [];
  const toasts: string[] = [];
  let complete: () => void = () => {};
  const completed = new Promise<void>((resolve) => { complete = resolve; });
  let advance: () => void = () => {};
  on('clock.every', async () => {
    await new Promise<void>((resolve) => { advance = resolve; });
    return { value: undefined };
  });
  on('env.get', ($, e) => ({ value: e.name === 'ISAAC_SMART_ROUTER_FEEDBACK' ? enabled : undefined }));
  on('tool.call', () => answer);
  on('ui.log', ($, e) => {
    logs.push(e.text);
    if (e.text.includes('unconfirmed')) complete();
    return { value: undefined };
  });
  on('ui.toast', ($, e) => { toasts.push(e.text); complete(); return { value: undefined }; });
  return { answer, logs, toasts, completed, tick: () => advance() };
}

test('background feedback returns immediately and notifies when a later status confirms delivery', async ($, on) => {
  const { logs, toasts, completed, tick } = setup(on);
  let checks = 0;
  on('process.run', ($, e) => {
    expect(e.argv).toEqual(['isaac', 'feedback', '--status', id]);
    checks += 1;
    return { value: { exitCode: 0, stderr: '', stdout: JSON.stringify({
      ...receipt, delivery_status: checks === 1 ? 'queued' : 'submitted',
    }) } };
  });

  const result = await $.tool.call({ tool: 'Bash', tool_use_id: 'feedback', command });
  expect(result.result.stderr).toBe('');
  expect(result.result.stdout).toContain('CLI diagnostic\n');
  expect(JSON.parse(result.result.stdout.split('\n')[1])).toEqual({ ...receipt, delivery_notifications: 'native' });
  expect(result.text).toBe(result.result.stdout);
  expect(logs).toEqual([`Sending feedback in the background. You can keep working. Feedback ID: ${id}`]);
  expect(toasts).toEqual([]);

  // A second call for the same report must not attach another monitor.
  await $.tool.call({ tool: 'Bash', tool_use_id: 'same-feedback', command });
  const next = await $.tool.call({ tool: 'Bash', tool_use_id: 'next-task', command: 'pwd' });
  expect(next.result.stdout).not.toContain('delivery_notifications');
  tick();
  await completed;
  expect(checks).toBe(2);
  expect(logs).toEqual([
    `Sending feedback in the background. You can keep working. Feedback ID: ${id}`,
    `Feedback submitted successfully. Feedback ID: ${id}`,
  ]);
  expect(toasts).toEqual(['Feedback submitted successfully ✅']);
});

for (const [name, status] of [
  ['failed upload', { ...receipt, delivery_status: 'failed' }],
  ['mismatched report', { ...receipt, feedback_id: '00000000-0000-0000-0000-000000000000', delivery_status: 'submitted' }],
] as const) {
  test(`${name} retains the original retry ID without claiming success`, async ($, on) => {
    const { logs, toasts, completed } = setup(on);
    on('process.run', () => ({ value: { exitCode: 0, stderr: '', stdout: JSON.stringify(status) } }));
    await $.tool.call({ tool: 'Bash', tool_use_id: 'feedback', command });
    await completed;
    expect(logs).toContain(`Feedback delivery is unconfirmed. Retry with /isaac-feedback retry ${id}.`);
    expect(toasts).toEqual([]);
  });
}

for (const [name, enabled, input] of [
  ['feedback disabled', '0', command],
  ['synchronous submission', '1', command.replace(' --background', '')],
  ['unrelated command', '1', 'pwd'],
] as const) {
  test(`${name} preserves the tool result without notifications or status calls`, async ($, on) => {
    const { answer, logs, toasts } = setup(on, enabled);
    let processes = 0;
    on('process.run', () => { processes += 1; return { deny: 'unexpected status query' }; });
    expect(await $.tool.call({ tool: 'Bash', tool_use_id: 'other', command: input })).toEqual(answer);
    expect(logs).toEqual([]);
    expect(toasts).toEqual([]);
    expect(processes).toBe(0);
  });
}

test('a denied Bash call remains denied and cannot send feedback', async ($, on) => {
  const logs: string[] = [];
  on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
  on('tool.call', () => ({ deny: 'user policy' }));
  expect(await $.tool.call({ tool: 'Bash', tool_use_id: 'denied', command })).toEqual({ deny: 'user policy' });
  expect(logs).toEqual([]);
});
