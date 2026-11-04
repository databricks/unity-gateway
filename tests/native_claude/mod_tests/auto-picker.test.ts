import { expect, test } from 'claude-code/testing';

const AUTO = {
  EXPOSE_AUTO_IN_MODEL_PICKER: '1',
  ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1', unset: [], recipe: 'test-recipe',
  orchestratorInstructions: 'Delegate under the orchestrator.',
};

test('Auto sessions reject stale smart-router commands without forwarding', {options: AUTO}, async ($, on) => {
  let forwarded = false;
  on('command.run', {command: 'smart-router'}, () => { forwarded = true; return {}; });
  for (const args of ['on', 'off', '']) {
    expect((await $.command.run({command: 'smart-router', args})).text).toContain('/model');
  }
  expect(forwarded).toBe(false);
});

test('Auto command forwards an empty argument to the native concrete picker', {options: AUTO}, async ($, on) => {
  let args: string | undefined;
  on('command.run', {command: 'model'}, ($, e) => { args = e.args; return {}; });
  on('session.model', () => ({value: 'sonnet'}));
  await $.command.run({command: 'model', args: 'auto'});
  expect(args).toBe('');
});
