import { expect, test } from 'claude-code/testing';

const options = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
  ENABLE_SMART_ROUTING_SAVINGS: '1',
  orchestratorInstructions: 'Delegate using Agent.',
  savings_pricer: ['/python', '-m', 'ucode.smart_routing.savings'],
  savings_usage: '/session/usage.json',
  recipe: 'task_v3',
  unset: [],
};

test('a denied savings read preserves orchestration, startup and model requests', { options }, async ($, on) => {
  const env: Record<string, string | undefined> = { ...options } as any;
  on('env.get', (_, e) => {
    if (e.name === 'ENABLE_SMART_ROUTING_SAVINGS') return { deny: 'savings read denied' };
    return { value: env[e.name] };
  });
  on('env.set', (_, e) => {
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('session.start', () => ({ cwd: '/session' }));
  on('prompt.submit', (_, e) => ({ text: e.text }));
  on('tool.describe', (_, e) => ({ description: e.description }));
  on('turn.complete', () => ({ text: 'done' }));
  let requests = 0;
  on('turn.step', async function* (_, e) {
    requests += 1;
    return { turnId: e.turnId, index: e.index, answer: 'done', toolUses: [] };
  });

  let denied = false;
  try {
    await $.env.get('ENABLE_SMART_ROUTING_SAVINGS');
  } catch {
    denied = true;
  }
  expect(denied).toBe(true);
  expect(await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/session' }))
    .toEqual({ cwd: '/session' });
  expect((await $.tool.describe({ tool: 'Agent', description: 'Native Agent description.' })).description)
    .toContain(options.orchestratorInstructions);
  expect((await $.prompt.submit({ text: 'hello' })).text).toBe('hello');
  for (const agentId of [undefined, 'child']) {
    const result = await $.turn.step({
      turnId: 'turn', index: 0, messageCount: 1, model: 'claude-sonnet-4-6', agentId,
    }).next();
    expect(result.done).toBe(true);
    expect(result.value.answer).toBe('done');
  }
  expect(requests).toBe(2);
  expect((await $.turn.complete({ answer: 'done' })).text).toBe('done');
});
