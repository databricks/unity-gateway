import { expect, mock, test } from 'claude-code/testing';

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

test('/model opens the native menu without an intermediary', {options: AUTO}, async ($, on) => {
  let forwarded = false;
  on('command.run', {command: 'model'}, ($, e) => {
    expect(e.args).toBe(''); forwarded = true; return {};
  });
  await $.command.run({command: 'model', args: ''});
  expect(forwarded).toBe(true);
});

test('Auto uses the existing smart-router footer without an Auto label', {options: AUTO}, async ($, on) => {
  mock.clock(on);
  on('env.get', ($, e) => ({value: e.name === 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY' ? '1' : undefined}));
  on('agent.list', () => ({value: []}));
  let tail;
  on('ui.render', {component: 'PromptHint'}, ($, e) => {
    tail = e.props.tail;
    return $.ui.resolve(e).Text({children: ['native hint']});
  });
  const row = await $.ui.mount({plugin: 'ug-smart-router', surface: 'terminal', component: 'PromptHint', props: {}});
  await row.drawn();
  expect(tail).toBe('smart router ON');
});
