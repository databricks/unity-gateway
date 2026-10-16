import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';

const USAGE = '/launch/savings-usage.json';
const PRICER = ['/ug/python', '-P', '-m', 'ucode.smart_routing.savings'];
const FLAGS = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
  ENABLE_SMART_ROUTING_SAVINGS: '1',
};
const OPTIONS = {
  ...FLAGS,
  unset: [],
  recipe: 'task_v3',
  savings_pricer: PRICER,
  savings_usage: USAGE,
  orchestrator_version: '1.4.0',
};
const ABOVE_PROMPT = { plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} } as const;
const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

// A subagent routed from Opus to Sonnet, as an earlier module instance wrote it.
const SAVED_USAGE = {
  version: 1,
  start_model: 'claude-opus-4-8',
  main_model: 'claude-opus-4-8',
  first_main_model: 'claude-opus-4-8',
  user_switched: false,
  entries: [
    {
      agent: 'a1',
      baseline: 'claude-opus-4-8',
      served: 'claude-sonnet-5',
      before_user_switch: true,
      usage: { input_tokens: 1000, output_tokens: 4000, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    },
  ],
};

// The engine beneath the mod: the launch environment, the saved sums, another plugin's
// row, and a pricer that prints whatever estimate the test last set.
function engine(on: On) {
  const env: Record<string, string | undefined> = { ...FLAGS };
  const pricer: { savings: unknown } = { savings: null };
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('session.start', ($, e) => ({ cwd: e.cwd }));
  on('session.model', () => ({ value: 'claude-opus-4-8' }));
  on('fs.read', ($, e) => (e.path === USAGE ? { value: JSON.stringify(SAVED_USAGE) } : { deny: 'missing' }));
  on('fs.write', () => ({ value: undefined }));
  on('process.run', () => ({
    value: { exitCode: 0, stdout: JSON.stringify({ savings: pricer.savings }), stderr: '' },
  }));
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['theirs'] }));
  return { pricer };
}

test('the savings row shows only while the pricer has an estimate', { options: OPTIONS }, async ($, on) => {
  const { pricer } = engine(on);
  const band = await $.ui.mount(ABOVE_PROMPT);

  // No estimate yet (no rates, or nothing rerouted): no row, not even the version.
  await $.session.start(start);
  let drawn = JSON.stringify(await band.drawn());
  expect(drawn).toContain('theirs');
  expect(drawn).not.toContain('Est. saved');
  expect(drawn).not.toContain('Unity Gateway Smart Router v');

  // A reload's session.start prices again.
  pricer.savings = { saved: true, amount: '$0.18', percent: '48%' };
  await $.session.start(start);
  drawn = JSON.stringify(await band.drawn());
  expect(drawn).toContain('theirs');
  expect(drawn).toContain('💰 Est. saved with smart routing');
  expect(drawn).toContain(' $0.18 ');
  expect(drawn).toContain('(48%)');
  expect(drawn).toContain('— Unity Gateway Smart Router v1.4.0');

  // A failed or empty pricing drops the stale estimate, and the row with it.
  pricer.savings = null;
  await $.session.start(start);
  drawn = JSON.stringify(await band.drawn());
  expect(drawn).toContain('theirs');
  expect(drawn).not.toContain('Est. saved');
});
