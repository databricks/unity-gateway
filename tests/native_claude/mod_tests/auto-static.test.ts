import type { On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';

// The static catalog mode: configure kept `replaceBuiltInOptions: true` and the
// native session model IS the catalog's single Auto row (the default variant alias).
// Native keeps that row selected for every main model, so /model checks Auto; the
// mod holds the main model (`staticModel` names the row, as the launch passes it).
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
  staticModel: DEFAULT_ALIAS,
  preferSaved: true,
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
function staticCatalogSession(on: On, initial: string, saved?: string) {
  let selected = initial;
  const store: Record<string, unknown> = saved ? { [AUTO.storeKey]: saved } : {};
  const commands: { command: string; args: string }[] = [];
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
  on('store.get', ($, e) => ({ value: store[e.key] }));
  on('store.set', ($, e) => {
    store[e.key] = e.value;
    return { value: undefined };
  });
  on('fs.read', () => ({ value: '{}' }));
  on('process.run', ($, e) => {
    runs.push(e.argv);
    return { value: { exitCode: 0, stderr: '', stdout: '' } };
  });
  on('command.register', () => ({ value: undefined }));
  on('command.run', ($, e) => {
    commands.push({ command: e.command, args: e.args });
    // Native /model <id> selects that model and appends its confirmation.
    if (e.command === 'model' && e.args) selected = e.args;
    return { value: { ref: 1, text: '' } };
  });
  on('ui.open', ($, e) => {
    opened.push(e.id);
    return { value: { isPlaced: true } };
  });
  on('ui.close', () => ({ value: undefined }));
  on('ui.toast', () => ({ value: undefined }));
  on('ui.invalidate', () => ({ value: undefined }));
  const clock = mock.clock(on);
  on('session.start', () => ({ cwd: '/work' }));
  return { env, writes, runs, opened, store, commands, clock, selected: () => selected };
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
    props: { title: 'Auto: choose your main model', isFocused: true, bodyColumns: 80, rows: 18, view: {} },
  } as never);
  const drawn = JSON.stringify(await pane.drawn());
  expect(drawn).toContain('Auto: choose your main model');
  expect(drawn).toContain("Subagents are now smart-routed. Pick the model for your main agent; it isn't routed.");
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
  staticCatalogSession(on, DEFAULT_ALIAS);
  const sent = captureSteps(on);
  await $.session.start(START);

  // The gateway serves no Auto alias, and native settings do not map a child's model.
  await step($, OTHER_ALIAS, 'child');
  await step($, DEFAULT_ALIAS, 'child');
  await step($, 'system.ai.claude-opus-5', 'child');
  expect(sent).toEqual([OTHER, MAIN, 'system.ai.claude-opus-5']);
});

function mountChooser($: any) {
  return $.ui.mount({
    plugin: 'ug-smart-router',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'auto-main',
    props: { title: 'Auto: choose your main model', isFocused: true, bodyColumns: 80, rows: 18, view: {} },
  } as never);
}

test('switching the main model keeps the static Auto row selected', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  const sent = captureSteps(on);
  await $.session.start(START);
  await $.command.run({ command: 'auto', args: '' });
  session.commands.length = 0;

  const pane = await mountChooser($);
  await pane.press({ key: 'auto-main-model-1' });
  await session.clock.advance(0);

  // No native /model switch: the checkmark stays on the one Auto row.
  expect(session.commands.filter((c) => c.command === 'model')).toEqual([]);
  expect(session.selected()).toBe(DEFAULT_ALIAS);
  expect(session.env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe(DEFAULT_ALIAS);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  // Main turns and unrouted children both send the chosen main model.
  await step($, DEFAULT_ALIAS);
  await step($, DEFAULT_ALIAS, 'child');
  expect(sent).toEqual([OTHER, OTHER]);
  expect(session.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

test('a fresh static Auto session starts on the saved main model', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS, OTHER);
  const sent = captureSteps(on);
  await $.session.start(START);

  await step($, DEFAULT_ALIAS);
  expect(sent).toEqual([OTHER]);
  expect(session.selected()).toBe(DEFAULT_ALIAS);
  await $.command.run({ command: 'auto', args: '' });
  expect(JSON.stringify(await (await mountChooser($)).drawn())).toContain('❯ Haiku 4.5');
});

test('a launch-only main model outranks the saved one', { options: { ...OPTIONS, autoPicker: JSON.stringify({ ...AUTO, preferSaved: false }) } }, async ($, on) => {
  staticCatalogSession(on, DEFAULT_ALIAS, OTHER);
  const sent = captureSteps(on);
  await $.session.start(START);

  await step($, DEFAULT_ALIAS);
  expect(sent).toEqual([MAIN]);
});

test('a per-model Auto alias saved by an earlier version still starts Auto', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, OTHER_ALIAS);
  const sent = captureSteps(on);
  await $.session.start(START);

  await step($, OTHER_ALIAS);
  expect(sent).toEqual([OTHER]);
  expect(session.runs.some((argv) => argv.includes('--enable-smart-routing'))).toBe(true);
  // Reselecting Auto from /model moves native onto the static row and keeps the main model.
  await $.command.run({ command: 'model', args: 'auto' });
  expect(session.selected()).toBe(DEFAULT_ALIAS);
  await $.session.append({
    door: 'command',
    message: { role: 'user', isMeta: false, content: [{ type: 'text', text: `<local-command-stdout>Set model to ${DEFAULT_ALIAS}` }] },
    origin: { kind: 'engine' },
  } as never);
  await step($, DEFAULT_ALIAS);
  expect(sent).toEqual([OTHER, OTHER]);
});
