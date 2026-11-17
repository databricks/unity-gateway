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
function staticCatalogSession(on: On, initial: string, saved?: string, contextTokens = 0) {
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
  const choices: {question:string; options:{label:string}[]}[] = [];
  const answers: (string | undefined)[] = [];
  const chooser: {answer?: () => Promise<string>} = {};
  on('tool.call', {tool:'AskUserQuestion'}, async ($, e, next) => {
    const question = e.questions[0];
    if (question.header !== 'Main model') return next(e);
    choices.push(question);
    const answer = chooser.answer ? await chooser.answer() : answers.shift();
    return {result:{answers:answer ? {[question.question]:answer} : {}}};
  });
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    writes.push(e.name);
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('session.model', () => ({ value: selected }));
  on('session.usage', () => ({ value: { context: { window: 200000, tokens: contextTokens } } }));
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
  on('ui.close', ($, e) => {
    return { value: undefined };
  });
  on('ui.toast', () => ({ value: undefined }));
  on('ui.invalidate', () => ({ value: undefined }));
  const clock = mock.clock(on);
  on('session.start', () => ({ cwd: '/work' }));
  on('session.end', () => ({sessionId:'session'}));
  return {
    env, writes, runs, opened, store, commands, clock, choices, answers, chooser,
    selected: () => selected,
    select: (model: string) => { selected = model; },
  };
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

test('/model auto opens a compact model question without effort controls or a pane', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({command:'model', args:'auto'});
  expect(session.choices).toEqual([]);
  await session.clock.advance(0);
  expect(session.opened).toEqual([]);
  expect(session.choices).toHaveLength(1);
  expect(session.choices[0].question).toBe('Auto: choose your main model');
  expect(session.choices[0].options.map(option => option.label)).toEqual(['Sonnet 5', 'Haiku 4.5']);
  expect(JSON.stringify(session.choices)).not.toContain('effort');
  expect(session.commands.filter(command => command.command === 'effort')).toEqual([]);
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

test('switching the main model keeps the static Auto row selected', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  const sent = captureSteps(on);
  await $.session.start(START);
  await $.command.run({ command: 'auto', args: '' });
  session.commands.length = 0;

  session.answers.push('Haiku 4.5');
  await session.clock.advance(0);

  // No native /model switch: the checkmark stays on the one Auto row.
  expect(session.commands.filter((c) => c.command === 'model' || c.command === 'effort')).toEqual([]);
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
  await session.clock.advance(0);
  expect(session.choices[0].options[0].label).toBe('Haiku 4.5');
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

// Records native command rows and the mod's visible transcript output.
function captureAppends(on: On): string[] {
  const rows: string[] = [];
  on('session.append', ($, e, next) => {
    rows.push(e.message.content.map((block: any) => block.text ?? '').join('\n'));
    return next(e);
  });
  on('ui.log', ($, e) => {
    expect(e.to).toBe('transcript');
    rows.push(e.text);
    return { value: undefined };
  });
  return rows;
}

const SET_STATIC = {
  door: 'command',
  message: { role: 'user', isMeta: false, content: [{ type: 'text', text: `<local-command-stdout>Set model to \`${DEFAULT_ALIAS}\`` }] },
  origin: { kind: 'engine' },
} as never;
// Native confirms Auto before the main-model chooser completes.
const NEUTRAL = '<local-command-stdout>Auto on: subagents smart-routed';
const savedConfirmation = (model: string) => `Auto mode: main model ${model.replace(/^system\.ai\./, '')}, subagents smart routed.`;

const ENTER_AUTO = {
  from_model: OTHER, to_model: DEFAULT_ALIAS, requested_model: DEFAULT_ALIAS,
  source:'picker', context_tokens:1000, prompt_cache_warm:true, cache_ttl:'5m',
  estimated_cache_write_usd:0.01, pricing:'catalog',
} as const;

test('entering static Auto keeps Haiku without the native cache warning', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, OTHER, undefined, 1000);
  on('classic.PreModelSwitch', () => ({}));
  const questions: string[] = [];
  on('tool.call', {tool:'AskUserQuestion'}, ($, e, next) => {
    if (e.questions[0].header === 'Main model') return next(e);
    questions.push(e.questions[0].question);
    return {result:{answers:{[e.questions[0].question]:'Yes, switch model'}}};
  });
  await $.session.start(START);
  await $.command.run({command:'model', args:''});
  expect((await $.classic.PreModelSwitch(ENTER_AUTO)).permissionDecision).toBe('allow');
  session.select(DEFAULT_ALIAS);
  await $.session.append(SET_STATIC);
  session.answers.push('Haiku 4.5');
  await session.clock.advance(0);
  expect(questions).toEqual([]);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
});

for (const answer of ['Yes, switch model', 'No, go back', undefined]) {
  test(`changing Auto's main model with existing context requires confirmation: ${answer ?? 'dismissed'}`, { options: OPTIONS }, async ($, on) => {
    const session = staticCatalogSession(on, DEFAULT_ALIAS, OTHER, 1000);
    const rows = captureAppends(on);
    const questions: string[] = [];
    on('tool.call', {tool:'AskUserQuestion'}, ($, e, next) => {
    if (e.questions[0].header === 'Main model') return next(e);
      questions.push(e.questions[0].question);
      expect(session.store[AUTO.storeKey]).toBe(OTHER);
      return {result:{answers:answer ? {[e.questions[0].question]:answer} : {}}};
    });
    await $.session.start(START);
    await $.command.run({command:'auto', args:''});
    session.answers.push('Sonnet 5');
    await session.clock.advance(0);
    expect(questions).toHaveLength(1);
    expect(questions[0]).toContain('Sonnet 5');
    expect(questions[0]).toContain('full conversation');
    const accepted = answer === 'Yes, switch model';
    expect(session.store[AUTO.storeKey]).toBe(accepted ? MAIN : OTHER);
    expect(rows).toEqual(accepted ? [savedConfirmation(MAIN)] : []);
    expect(session.selected()).toBe(DEFAULT_ALIAS);
    expect(session.choices).toHaveLength(accepted ? 1 : 2);
  });
}

test('a fresh Auto model change needs no context warning', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS, OTHER);
  let asked = false;
  on('tool.call', {tool:'AskUserQuestion'}, ($, e, next) => {
    if (e.questions[0].header === 'Main model') return next(e);
    asked = true; return {result:{}};
  });
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.answers.push('Sonnet 5');
  await session.clock.advance(0);
  expect(asked).toBe(false);
  expect(session.store[AUTO.storeKey]).toBe(MAIN);
});

test('a confirmation overtaken by a new model command cannot save the old choice', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS, OTHER, 1000);
  const rows = captureAppends(on);
  let started: () => void;
  let release: () => void;
  const ready = new Promise<void>(resolve => { started = resolve; });
  on('tool.call', {tool:'AskUserQuestion'}, async ($, e, next) => {
    if (e.questions[0].header === 'Main model') return next(e);
    await new Promise<void>(resolve => { release = resolve; started(); });
    return {result:{answers:{[e.questions[0].question]:'Yes, switch model'}}};
  });
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.answers.push('Sonnet 5');
  const saving = session.clock.advance(0);
  await ready;
  await $.command.run({command:'model', args:OTHER});
  release!();
  await saving;
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  expect(session.selected()).toBe(OTHER);
  expect(rows).toEqual([]);
});

test('saving an inactive Auto preference does not warn about the running model', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, OTHER, OTHER, 1000);
  let asked = false;
  on('tool.call', {tool:'AskUserQuestion'}, ($, e, next) => {
    if (e.questions[0].header === 'Main model') return next(e);
    asked = true; return {result:{}};
  });
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.answers.push('Sonnet 5');
  await session.clock.advance(0);
  expect(asked).toBe(false);
  expect(session.selected()).toBe(OTHER);
  expect(session.store[AUTO.storeKey]).toBe(MAIN);
});

for (const decision of ['deny', 'ask'] as const) {
  test(`entering static Auto preserves another hook's ${decision}`, { options: OPTIONS }, async ($, on) => {
    staticCatalogSession(on, OTHER, undefined, 1000);
    on('classic.PreModelSwitch', () => ({permissionDecision:decision}));
    await $.session.start(START);
    await $.command.run({command:'model', args:''});
    expect((await $.classic.PreModelSwitch(ENTER_AUTO)).permissionDecision).toBe(decision);
  });
}

test('choosing a main model confirms the saved model and sends the new one', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  const sent = captureSteps(on);
  const rows = captureAppends(on);
  await $.session.start(START);
  await $.command.run({ command: 'model', args: 'auto' });
  await $.session.append(SET_STATIC);
  expect(rows).toEqual([NEUTRAL]);

  session.answers.push('Haiku 4.5');
  await session.clock.advance(0);
  expect(rows).toEqual([NEUTRAL, savedConfirmation(OTHER)]);
  await step($, DEFAULT_ALIAS);
  await step($, DEFAULT_ALIAS, 'child');
  expect(sent).toEqual([OTHER, OTHER]);
});

test('a late native Auto confirmation preserves the saved model confirmation', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  const sent = captureSteps(on);
  const rows = captureAppends(on);
  await $.session.start(START);
  // /model auto opens the chooser at once; its confirmation may arrive after the pick.
  await $.command.run({ command: 'model', args: 'auto' });
  session.answers.push('Haiku 4.5');
  await session.clock.advance(0);
  await $.session.append(SET_STATIC);
  await session.clock.advance(0);

  expect(rows).toEqual([savedConfirmation(OTHER), NEUTRAL]);
  expect(session.choices).toHaveLength(1);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  await step($, DEFAULT_ALIAS);
  await step($, DEFAULT_ALIAS, 'child');
  expect(sent).toEqual([OTHER, OTHER]);
});

test('nonstatic Auto confirms a changed main model only after native accepts it', {
  options: { ...OPTIONS, autoPicker: JSON.stringify({ ...AUTO, staticModel: undefined }) },
}, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  const rows = captureAppends(on);
  await $.session.start(START);
  await $.command.run({ command: 'model', args: 'auto' });
  await $.session.append(SET_STATIC);
  expect(rows).toEqual([NEUTRAL]);

  session.answers.push('Haiku 4.5');
  await session.clock.advance(0);
  expect(rows).toEqual([NEUTRAL]);
  await $.session.append({
    ...SET_STATIC,
    message: { role: 'user', isMeta: false, content: [{ type: 'text', text: `<local-command-stdout>Set model to \`${OTHER_ALIAS}\`` }] },
    origin: { kind: 'plugin', name: 'ug-smart-router' },
  } as never);
  await session.clock.advance(0);

  expect(rows).toEqual([NEUTRAL, NEUTRAL, savedConfirmation(OTHER)]);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
});

// The tail the mod hands the engine for the prompt hint row under the input.
async function hintTail($: any, on: On): Promise<() => Promise<unknown>> {
  const tails: unknown[] = [];
  on('agent.list', () => ({ value: [] }));
  on('ui.render', { component: 'PromptHint' }, ($, e) => {
    tails.push(e.props.tail);
    return $.ui.resolve(e).Text({ children: ['hint'] });
  });
  return async () => {
    tails.length = 0;
    await $.ui.mount({ plugin: 'ug-smart-router', surface: 'terminal', component: 'PromptHint', props: { tail: '' } } as never);
    return tails.at(-1);
  };
}

test('the hint row names the main model chosen under the static Auto row', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  const hint = await hintTail($, on);
  await $.session.start(START);
  expect(await hint()).toBe('Auto (Sonnet 5) · smart router ON');

  await $.command.run({ command: 'auto', args: '' });
  session.answers.push('Haiku 4.5');
  await session.clock.advance(0);

  // Native still selects the default variant's alias; the row follows the mod's main model.
  expect(session.selected()).toBe(DEFAULT_ALIAS);
  expect(await hint()).toBe('Auto (Haiku 4.5) · smart router ON');
});

test('a fresh static Auto session names the saved main model in the hint row', { options: OPTIONS }, async ($, on) => {
  staticCatalogSession(on, DEFAULT_ALIAS, OTHER);
  const hint = await hintTail($, on);
  await $.session.start(START);
  expect(await hint()).toBe('Auto (Haiku 4.5) · smart router ON');
});

// Native 2.1.325's order, from a live transcript: /model returns its panel ref,
// native appends the static row's confirmation as its picker closes, and only
// then does the person pick a main model in the mod's chooser.
test('each /model main-model save confirms the newly selected model', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  const sent = captureSteps(on);
  const rows = captureAppends(on);
  const hint = await hintTail($, on);
  await $.session.start(START);

  for (const [model, label] of [[OTHER, 'Haiku 4.5'], [MAIN, 'Sonnet 5']] as const) {
    rows.length = 0;
    await $.command.run({ command: 'model', args: '' });
    await $.session.append(SET_STATIC);
    session.answers.push(label);
    await session.clock.advance(0);
    expect(rows).toEqual([NEUTRAL, savedConfirmation(model)]);
    expect(await hint()).toBe(`Auto (${label}) · smart router ON`);
    sent.length = 0;
    await step($, DEFAULT_ALIAS);
    await step($, DEFAULT_ALIAS, 'child');
    expect(sent).toEqual([model, model]);
    expect(session.store[AUTO.storeKey]).toBe(model);
  }
});

test('leaving the chooser without a pick appends no notice and keeps the main model', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS, OTHER);
  const sent = captureSteps(on);
  const rows = captureAppends(on);
  const hint = await hintTail($, on);
  await $.session.start(START);
  await $.command.run({ command: 'model', args: '' });
  await $.session.append(SET_STATIC);
  await session.clock.advance(0);

  expect(rows).toEqual([NEUTRAL]);
  expect(await hint()).toBe('Auto (Haiku 4.5) · smart router ON');
  await step($, DEFAULT_ALIAS);
  expect(sent).toEqual([OTHER]);
});

function commandRow(text: string, origin: unknown) {
  return { door: 'command', message: { role: 'user', isMeta: false, content: [{ type: 'text', text }] }, origin } as never;
}

test('native effort command output is preserved while Auto is active', { options: OPTIONS }, async ($, on) => {
  staticCatalogSession(on, DEFAULT_ALIAS);
  const rows = captureAppends(on);
  await $.session.start(START);
  const echo = '<command-name>/effort</command-name>\n<command-message>effort</command-message>\n<command-args>status</command-args>';
  const output = '<local-command-stdout>Current effort level: low</local-command-stdout>';
  for (const origin of [{kind:'composer'}, {kind:'plugin', name:'ug-smart-router'}]) {
    rows.length = 0;
    await $.session.append(commandRow(echo, origin));
    await $.session.append(commandRow(output, origin));
    expect(rows).toEqual([echo, output]);
  }
});

// Native 2.1.325's /model returns its panel ref while the picker is still up, before
// the person picks a row; only the Auto row's confirmation may open the chooser.
const commandOutput = (text: string) => commandRow(`<local-command-stdout>${text}`, { kind: 'engine' });

test('opening /model without a pick leaves the chooser closed', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({ command: 'model', args: '' });
  await session.clock.advance(0);
  expect(session.choices).toEqual([]);
});

test('picking a non-Auto model in /model never opens the chooser', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({ command: 'model', args: '' });
  session.select('system.ai.claude-opus-5');
  await $.session.append(commandOutput('Set model to Claude Opus 5'));
  await session.clock.advance(0);
  expect(session.choices).toEqual([]);
});

test('cancelling /model never opens the chooser', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({ command: 'model', args: '' });
  await $.session.append(commandOutput(`Kept model as \`${DEFAULT_ALIAS}\``));
  await session.clock.advance(0);
  expect(session.choices).toEqual([]);
});

test('picking Auto in /model opens the chooser once its confirmation arrives', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, 'system.ai.claude-opus-5');
  await $.session.start(START);
  await $.command.run({ command: 'model', args: '' });
  expect(session.choices).toEqual([]);
  session.select(DEFAULT_ALIAS);
  await $.session.append(SET_STATIC);
  expect(session.choices).toEqual([]);
  await session.clock.advance(0);
  expect(session.choices).toHaveLength(1);
  expect(session.choices[0].question).toBe('Auto: choose your main model');
});

test('/model auto opens the chooser directly', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  await $.session.start(START);
  expect(session.choices).toEqual([]);
  await $.command.run({ command: 'model', args: 'auto' });
  await session.clock.advance(0);
  expect(session.choices).toHaveLength(1);
});

const LARGE_AUTO = {
  ...AUTO,
  models: Array.from({length:12}, (_, index) => index < 2 ? AUTO.models[index] : {
    model:`system.ai.model-${index}`, label:`Model ${index}`, autoModel:`ug-auto-model-${index}`,
  }),
};

test('the compact chooser pages through every model with at most four options', {
  options:{...OPTIONS, autoPicker:JSON.stringify(LARGE_AUTO)},
}, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS, OTHER);
  const rows = captureAppends(on);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.answers.push('More models', 'More models', 'More models', 'Model 11');
  await session.clock.advance(0);
  expect(session.choices).toHaveLength(4);
  expect(session.choices.map(question => question.options.length)).toEqual([4, 4, 4, 4]);
  expect(session.choices[0].options[0].label).toBe('Haiku 4.5');
  expect(session.choices.flatMap(question => question.options.map(option => option.label).filter(label => label !== 'More models')))
    .toEqual(['Haiku 4.5', 'Sonnet 5', ...Array.from({length:10}, (_, index) => `Model ${index + 2}`)]);
  expect(session.store[AUTO.storeKey]).toBe('system.ai.model-11');
  expect(rows).toEqual([savedConfirmation('system.ai.model-11')]);
});

test('More models cycles back to the current model on the first page', {
  options:{...OPTIONS, autoPicker:JSON.stringify(LARGE_AUTO)},
}, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS, OTHER);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.answers.push('More models', 'More models', 'More models', 'More models', 'Haiku 4.5');
  await session.clock.advance(0);
  expect(session.choices).toHaveLength(5);
  expect(session.choices[4].options).toEqual(session.choices[0].options);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
});

for (const answer of ['Sonnet 5', 'Cancel']) {
  test(`a single main model has a valid compact question: ${answer}`, {
    options:{...OPTIONS, autoPicker:JSON.stringify({...AUTO, models:[AUTO.models[0]]})},
  }, async ($, on) => {
    const session = staticCatalogSession(on, DEFAULT_ALIAS);
    const rows = captureAppends(on);
    await $.session.start(START);
    await $.command.run({command:'auto', args:''});
    session.answers.push(answer);
    await session.clock.advance(0);
    expect(session.choices[0].options.map(option => option.label)).toEqual(['Sonnet 5', 'Cancel']);
    expect(rows).toEqual(answer === 'Cancel' ? [] : [savedConfirmation(MAIN)]);
  });
}

const AMBIGUOUS_AUTO = {
  ...AUTO, models:[
    {...AUTO.models[0], label:'Shared'}, {...AUTO.models[1], label:'Shared'},
    {model:'system.ai.third', label:'More models', autoModel:'ug-auto-third'},
    {model:'system.ai.fourth', label:'Cancel', autoModel:'ug-auto-fourth'},
  ],
};

test('duplicate and navigation labels identify their actual model', {
  options:{...OPTIONS, autoPicker:JSON.stringify(AMBIGUOUS_AUTO)},
}, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.answers.push(`Shared (${OTHER})`);
  await session.clock.advance(0);
  expect(session.choices[0].options.map(option => option.label)).toEqual([
    `Shared (${MAIN})`, `Shared (${OTHER})`, 'More models (system.ai.third)', 'Cancel (system.ai.fourth)',
  ]);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
});

for (const answer of [undefined, 'Cancel']) {
  test(`an invalid typed model shows an error before ${answer ?? 'dismissal'}`, {options:OPTIONS}, async ($, on) => {
    const session = staticCatalogSession(on, DEFAULT_ALIAS, MAIN);
    const rows = captureAppends(on);
    await $.session.start(START);
    await $.command.run({command:'auto', args:''});
    session.answers.push('unavailable model', answer);
    await session.clock.advance(0);
    expect(session.choices).toHaveLength(2);
    expect(session.choices[1].question).toContain('Invalid model. Choose a model from the list.');
    expect(session.choices[1].options).toEqual(session.choices[0].options);
    expect(session.store[AUTO.storeKey]).toBe(MAIN);
    expect(rows).toEqual([]);
  });
}

test('a valid choice after an invalid typed model saves once', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS, MAIN);
  const rows = captureAppends(on);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.answers.push('unavailable model', 'Haiku 4.5');
  await session.clock.advance(0);
  expect(session.choices).toHaveLength(2);
  expect(session.choices[1].question).toContain('Invalid model. Choose a model from the list.');
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  expect(rows).toEqual([savedConfirmation(OTHER)]);
});

test('repeated invalid typed models preserve the current page until cancellation', {
  options:{...OPTIONS, autoPicker:JSON.stringify(LARGE_AUTO)},
}, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS, MAIN);
  const rows = captureAppends(on);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.answers.push('More models', 'unavailable model', 'another unavailable model', undefined);
  await session.clock.advance(0);
  expect(session.choices).toHaveLength(4);
  expect(session.choices[1].options.map(option => option.label)).toEqual(['Model 3', 'Model 4', 'Model 5', 'More models']);
  for (const question of session.choices.slice(2)) {
    expect(question.question).toContain('Invalid model. Choose a model from the list.');
    expect(question.options).toEqual(session.choices[1].options);
  }
  expect(session.store[AUTO.storeKey]).toBe(MAIN);
  expect(rows).toEqual([]);
});

test('dismissing the chooser does not show an invalid-model error', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession(on, DEFAULT_ALIAS, MAIN);
  const rows = captureAppends(on);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  await session.clock.advance(0);
  expect(session.choices).toHaveLength(1);
  expect(session.choices[0].question).toBe('Auto: choose your main model');
  expect(session.store[AUTO.storeKey]).toBe(MAIN);
  expect(rows).toEqual([]);
});

for (const interrupt of ['model', 'prompt', 'end'] as const) {
  test(`an old chooser answer after a new ${interrupt} cannot save`, {options:OPTIONS}, async ($, on) => {
    const session = staticCatalogSession(on, DEFAULT_ALIAS, MAIN);
    const rows = captureAppends(on);
    let release: () => void;
    let started: () => void;
    const ready = new Promise<void>(resolve => { started = resolve; });
    session.chooser.answer = async () => {
      await new Promise<void>(resolve => { release = resolve; started(); });
      return 'Haiku 4.5';
    };
    await $.session.start(START);
    await $.command.run({command:'auto', args:''});
    const choosing = session.clock.advance(0);
    await ready;
    if (interrupt === 'model') await $.command.run({command:'model', args:MAIN});
    if (interrupt === 'prompt') await $.session.append({door:'prompt', message:{role:'user', isMeta:false, content:[{type:'text', text:'Continue'}]}, origin:{kind:'composer'}} as never);
    if (interrupt === 'end') await $.session.end({reason:'prompt_input_exit'});
    rows.length = 0;
    release!();
    await choosing;
    expect(session.store[AUTO.storeKey]).toBe(MAIN);
    expect(rows).toEqual([]);
  });
}

test('direct /model auto keeps the entry model before a delayed native confirmation', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession(on, OTHER, undefined, 1000);
  const rows = captureAppends(on);
  let warned = false;
  on('tool.call', {tool:'AskUserQuestion'}, ($, e, next) => {
    if (e.questions[0].header === 'Main model') return next(e);
    warned = true;
    return {result:{answers:{}}};
  });
  await $.session.start(START);
  await $.command.run({command:'model', args:'auto'});
  session.answers.push('Haiku 4.5');
  await session.clock.advance(0);
  expect(session.choices[0].options[0].label).toBe('Haiku 4.5');
  expect(warned).toBe(false);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  await $.session.append(SET_STATIC);
  await session.clock.advance(0);
  expect(session.choices).toHaveLength(1);
  expect(rows).toEqual([savedConfirmation(OTHER), NEUTRAL]);
});
