import type { On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';

// The static catalog mode: configure kept `replaceBuiltInOptions: true` and the
// native session model IS the catalog's single Auto row (the default variant alias).
const MAIN = 'system.ai.claude-sonnet-5';
const OTHER = 'system.ai.claude-haiku-4-5';
const DEFAULT_ALIAS = 'ug-auto-111111111111-claude-sonnet-5';
const OTHER_ALIAS = 'ug-auto-222222222222-claude-haiku-4-5';
const DESCRIPTION = 'Delivers frontier-model quality at lower cost through Smart Routing.';
const AUTO = {
  model: DEFAULT_ALIAS,
  mainModel: MAIN,
  storeKey: 'main-model-static',
  description: DESCRIPTION,
  models: [
    { model: MAIN, label: 'Sonnet 5', autoModel: DEFAULT_ALIAS },
    { model: OTHER, label: 'Haiku 4.5', autoModel: OTHER_ALIAS },
  ],
};
const RECIPE = 'task_v3';
const OPTIONS = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '0',
  ENABLE_SMART_ROUTER_AUTO_MODE: '1',
  autoPicker: JSON.stringify(AUTO),
  recipe: RECIPE,
  unset: [],
};
const START = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

// The engine answers everything the Auto flow reads; the native session model is
// the static Auto row's alias, as a replacement picker's selected row resolves to.
function staticCatalogSession(on: On, selected: string) {
  const env: Record<string, string | undefined> = {
    ENABLE_SMART_ROUTING_V2: '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: '0',
    ENABLE_SMART_ROUTER_AUTO_MODE: '1',
    UCODE_SMART_ROUTER_PYTHON: '/ug/python',
    UCODE_SESSION_ENV_FILE: '/session/env.json',
  };
  const writes: string[] = [];
  const runs: readonly string[][] = [];
  const opened: string[] = [];
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    writes.push(e.name);
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('session.model', () => ({ value: selected }));
  on('session.usage', () => ({ value: { context: { window: 200000 } } }));
  on('settings.read', () => ({
    value: { modelOverrides: { [DEFAULT_ALIAS]: MAIN, [OTHER_ALIAS]: OTHER } },
  }));
  on('store.get', () => ({ value: undefined }));
  on('store.set', () => ({ value: undefined }));
  on('fs.read', () => ({ value: '{}' }));
  on('process.run', ($, e) => {
    runs.push(e.argv);
    return { value: { exitCode: 0, stderr: '', stdout: '' } };
  });
  on('command.register', () => ({ value: undefined }));
  on('command.run', () => ({ value: { ref: 1, text: '' } }));
  on('ui.open', ($, e) => {
    opened.push(e.id);
    return { value: { isPlaced: true } };
  });
  on('ui.close', () => ({ value: undefined }));
  on('ui.toast', () => ({ value: undefined }));
  on('ui.invalidate', () => ({ value: undefined }));
  mock.clock(on);
  on('session.start', () => ({ cwd: '/work' }));
  return { env, writes, runs, opened };
}

test('the static catalog Auto row activates routing and reuses its own alias as the custom option', { options: OPTIONS }, async ($, on) => {
  const { env, writes, runs } = staticCatalogSession(on, DEFAULT_ALIAS);

  await $.session.start(START);

  // The mod re-points the process custom option at the same variant the static
  // row already shows: one Auto identity, no duplicate entry, and `/model auto`
  // switching can validate against it.
  expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe(DEFAULT_ALIAS);
  expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION_NAME).toBe('Auto');
  expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION).toBe(DESCRIPTION);
  expect(writes.filter((name) => name === 'ANTHROPIC_CUSTOM_MODEL_OPTION')).toHaveLength(1);
  // Selecting the Auto row routes: the flags stay on and the session controls are
  // initialized for the process.
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(runs.some((argv) => argv.includes('--enable-smart-routing'))).toBe(true);
  expect(JSON.stringify(runs)).toContain('"--enable-smart-routing"');
});

test('/model auto opens the main-model chooser listing every catalog row', { options: OPTIONS }, async ($, on) => {
  const { opened } = staticCatalogSession(on, DEFAULT_ALIAS);

  await $.session.start(START);
  opened.length = 0;
  await $.command.run({ command: 'model', args: 'auto' });

  expect(opened, `ui.open calls: ${JSON.stringify(opened)}`).toContain('auto-main');
  // Draw the pane the mod opened; its render lists one row per main model.
  const pane = await $.ui.mount({
    plugin: 'ug-smart-router',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'auto-main',
    props: { title: 'Choose your main model', isFocused: true, bodyColumns: 80, rows: 18, view: {} },
  } as never);
  const drawn = JSON.stringify(await pane.drawn());
  expect(drawn).toContain('Choose your main model');
  expect(drawn).toContain('Sonnet 5');
  expect(drawn).toContain('Haiku 4.5');
  // The current main model carries the focus marker.
  expect(drawn).toContain('❯ Sonnet 5');
});

// The engine's bottom of the turn.step chain; returns the model each step sends.
function captureSteps(on: On): string[] {
  const sent: string[] = [];
  on('turn.step', async function* ($, e) {
    sent.push(e.model);
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null };
  });
  return sent;
}

async function step($: any, model: string, agentId?: string): Promise<void> {
  const stream = $.turn.step({ turnId: 'turn', index: 0, messageCount: 1, model, agentId });
  for (let item = await stream.next(); !item.done; item = await stream.next()) {}
}

test('an unrouted subagent that inherits an Auto alias sends its real target', { options: OPTIONS }, async ($, on) => {
  staticCatalogSession(on, OTHER_ALIAS);
  const sent = captureSteps(on);
  await $.session.start(START);

  // The gateway serves no Auto alias, and native settings do not map a child's model.
  await step($, OTHER_ALIAS, 'child');
  await step($, DEFAULT_ALIAS, 'child');
  await step($, 'system.ai.claude-opus-5', 'child');
  expect(sent).toEqual([OTHER, MAIN, 'system.ai.claude-opus-5']);
});
