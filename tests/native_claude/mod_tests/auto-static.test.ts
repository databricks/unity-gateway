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
const OPTIONS = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '0',
  ENABLE_SMART_ROUTER_AUTO_MODE: '1',
  autoPicker: JSON.stringify(AUTO),
  unset: [],
};
const START = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

// The engine answers everything the Auto flow reads; the native session model is
// the static Auto row's alias, as a replacement picker's selected row resolves to.
function staticCatalogSession($: any, on: On, initial: string, saved?: string, contextTokens = 0) {
  let selected = initial;
  const store: Record<string, unknown> = saved ? { [AUTO.storeKey]: saved } : {};
  const commands: { command: string; args: string }[] = [];
  const env: Record<string, string | undefined> = {
    UCODE_AGENT_LABELS_DIR: '/plugin-data',
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
  const closed: string[] = [];
  const toasts: string[] = [];
  const chooser: {
    beforeModel?: () => Promise<void>;
    beforeOpen?: () => Promise<void>;
    beforeClose?: () => void;
    beforeStore?: () => void;
  } = {};
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    writes.push(e.name);
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('session.model', async () => { await chooser.beforeModel?.(); return {value:selected}; });
  on('session.usage', () => ({ value: { context: { window: 200000, tokens: contextTokens } } }));
  on('settings.read', () => ({
    value: { modelOverrides: { [DEFAULT_ALIAS]: MAIN, [OTHER_ALIAS]: OTHER } },
  }));
  on('store.get', ($, e) => ({ value: store[e.key] }));
  on('session.id', () => ({ value: 'auto-static-session' }));
  on('fs.exists', () => ({ value: false }));
  on('store.set', ($, e) => {
    chooser.beforeStore?.();
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
  on('ui.open', async ($, e) => {
    if (e.id === 'auto-models') await chooser.beforeOpen?.();
    opened.push(e.id);
    return { value: { isPlaced: true } };
  });
  on('ui.close', ($, e) => {
    closed.push(e.id);
    chooser.beforeClose?.();
    return { value: undefined };
  });
  on('ui.render', {component:'Pane'}, ($, e) => $.ui.resolve(e).Box({children:[]}));
  on('ui.toast', ($, e) => { toasts.push(e.text); return {value:undefined}; });
  on('ui.invalidate', () => ({ value: undefined }));
  const clock = mock.clock(on);
  on('session.start', () => ({ cwd: '/work' }));
  on('session.end', () => ({sessionId:'session'}));
  return {
    env, writes, runs, opened, closed, toasts, store, commands, clock, chooser,
    async picker() {
      return $.ui.mount({
        plugin:'ug-smart-router', component:'Pane', requestId:'auto-models', surface:'terminal',
        props:{title:'Auto mode: Choose your main model', isFocused:true, bodyColumns:80, placement:'inline', scroll:{offset:0,bodyRows:8}, view:{}},
      });
    },
    async choose(model: string) {
      const picker = await this.picker();
      const select = await picker.find({type:'Select'});
      await picker.select({key:select.props.key, value:model});
      await picker.unmount();
    },
    selected: () => selected,
    setSelected: (model: string) => { selected = model; },
  };
}

test('the static catalog Auto row activates routing and reuses its own alias as the custom option', { options: OPTIONS }, async ($, on) => {
  const { env, writes, runs } = staticCatalogSession($, on, DEFAULT_ALIAS);

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

test('/model auto opens a focused Select with the current model first', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({command:'model', args:'auto'});
  const picker = await session.picker();
  const select = await picker.find({type:'Select'});
  expect(select.props.options).toEqual([{value:MAIN, label:'Sonnet 5'}, {value:OTHER, label:'Haiku 4.5'}]);
  const heading = await picker.find({type:'Text'});
  expect(heading.children).toEqual(['Auto mode: Choose your main model (currently Sonnet 5).']);
  expect(select.props.autoFocus).toBe(true);
  expect(select.props.value).toBe(MAIN);
  await picker.unmount();
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
  staticCatalogSession($, on, DEFAULT_ALIAS);
  const sent = captureSteps(on);
  await $.session.start(START);

  // The gateway serves no Auto alias, and native settings do not map a child's model.
  await step($, OTHER_ALIAS, 'child');
  await step($, DEFAULT_ALIAS, 'child');
  await step($, 'system.ai.claude-opus-5', 'child');
  expect(sent).toEqual([OTHER, MAIN, 'system.ai.claude-opus-5']);
});

test('a fresh static Auto session starts on the saved main model', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS, OTHER);
  const sent = captureSteps(on);
  const hint = await hintTail($, on);
  await $.session.start(START);
  expect(await hint()).toBe('Auto (Haiku 4.5) · smart router ON');

  await step($, DEFAULT_ALIAS);
  expect(sent).toEqual([OTHER]);
  expect(session.selected()).toBe(DEFAULT_ALIAS);
  await $.command.run({ command: 'auto', args: '' });
  await session.clock.advance(0);
  expect(session.toasts).toEqual([]);
  expect((await (await session.picker()).find({type:'Select'})).props.options[0].label).toBe('Haiku 4.5');
});

test('a launch-only main model outranks the saved one', { options: { ...OPTIONS, autoPicker: JSON.stringify({ ...AUTO, preferSaved: false }) } }, async ($, on) => {
  staticCatalogSession($, on, DEFAULT_ALIAS, OTHER);
  const sent = captureSteps(on);
  await $.session.start(START);

  await step($, DEFAULT_ALIAS);
  expect(sent).toEqual([MAIN]);
});

test('a per-model Auto alias saved by an earlier version still starts Auto', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, OTHER_ALIAS);
  const sent = captureSteps(on);
  await $.session.start(START);

  await step($, OTHER_ALIAS);
  expect(sent).toEqual([OTHER]);
  expect(session.runs.some((argv) => argv.includes('--enable-smart-routing'))).toBe(true);
  // Reselecting Auto from /model moves native onto the static row and keeps the main model.
  await $.command.run({ command: 'model', args: 'auto' });
  expect(session.selected()).toBe(DEFAULT_ALIAS);

  await step($, DEFAULT_ALIAS);
  expect(sent).toEqual([OTHER, OTHER]);
});

function captureNotices(on: On): string[] {
  const rows: string[] = [];
  on('ui.log', ($, e) => {
    expect(e.to).toBe('transcript');
    rows.push(e.text);
    return { value: undefined };
  });
  return rows;
}

const ENTER_AUTO = {
  from_model: OTHER, to_model: DEFAULT_ALIAS, requested_model: DEFAULT_ALIAS,
  source:'picker', context_tokens:1000, prompt_cache_warm:true, cache_ttl:'5m',
  estimated_cache_write_usd:0.01, pricing:'catalog',
} as const;

test('entering static Auto keeps Haiku without the native cache warning', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, OTHER, undefined, 1000);
  on('classic.PreModelSwitch', () => ({}));
  const questions: string[] = [];
  on('tool.call', {tool:'AskUserQuestion'}, ($, e) => {
    questions.push(e.questions[0].question);
    return {result:{answers:{[e.questions[0].question]:'Yes, switch model'}}};
  });
  await $.session.start(START);
  await $.command.run({command:'model', args:''});
  expect((await $.classic.PreModelSwitch(ENTER_AUTO)).permissionDecision).toBe('allow');
  await $.command.run({command:'model', args:'auto'});
  await session.choose(OTHER);
  expect(questions).toEqual([]);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
});

for (const answer of ['Yes, switch model', 'No, go back', undefined]) {
  test(`changing Auto's main model with existing context requires confirmation: ${answer ?? 'dismissed'}`, { options: OPTIONS }, async ($, on) => {
    const session = staticCatalogSession($, on, DEFAULT_ALIAS, OTHER, 1000);
    const rows = captureNotices(on);
    const questions: string[] = [];
    on('tool.call', {tool:'AskUserQuestion'}, ($, e) => {
      questions.push(e.questions[0].question);
      expect(session.store[AUTO.storeKey]).toBe(OTHER);
      return {result:{answers:answer ? {[e.questions[0].question]:answer} : {}}};
    });
    await $.session.start(START);
    await $.command.run({command:'auto', args:''});
    await session.choose(MAIN);
    expect(questions).toHaveLength(1);
    expect(questions[0]).toContain('Sonnet 5');
    expect(questions[0]).toContain('full conversation');
    const accepted = answer === 'Yes, switch model';
    expect(session.store[AUTO.storeKey]).toBe(accepted ? MAIN : OTHER);
    expect(rows).toEqual([]);
    expect(session.selected()).toBe(DEFAULT_ALIAS);
    if (!accepted) {
      expect(session.closed).not.toContain('auto-models');
      await session.choose(OTHER);
    }
    expect(session.closed).toContain('auto-models');
  });
}

test('a fresh Auto model change needs no context warning', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS, OTHER);
  let asked = false;
  on('tool.call', {tool:'AskUserQuestion'}, ($, e) => {
    asked = true; return {result:{}};
  });
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  await session.choose(MAIN);
  expect(asked).toBe(false);
  expect(session.store[AUTO.storeKey]).toBe(MAIN);
});

test('a confirmation overtaken by a new model command cannot save the old choice', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS, OTHER, 1000);
  const rows = captureNotices(on);
  let started: () => void;
  let release: () => void;
  const ready = new Promise<void>(resolve => { started = resolve; });
  on('tool.call', {tool:'AskUserQuestion'}, async ($, e) => {
    await new Promise<void>(resolve => { release = resolve; started(); });
    return {result:{answers:{[e.questions[0].question]:'Yes, switch model'}}};
  });
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  const saving = session.choose(MAIN);
  await ready;
  await $.command.run({command:'model', args:OTHER});
  release!();
  await saving;
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  expect(session.selected()).toBe(OTHER);
  expect(rows).toEqual([]);
});

test('saving an inactive Auto preference does not warn about the running model', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, OTHER, OTHER, 1000);
  let asked = false;
  on('tool.call', {tool:'AskUserQuestion'}, ($, e) => {
    asked = true; return {result:{}};
  });
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  await session.choose(MAIN);
  expect(asked).toBe(false);
  expect(session.selected()).toBe(OTHER);
  expect(session.store[AUTO.storeKey]).toBe(MAIN);
});

for (const decision of ['deny', 'ask'] as const) {
  test(`entering static Auto preserves another hook's ${decision}`, { options: OPTIONS }, async ($, on) => {
    staticCatalogSession($, on, OTHER, undefined, 1000);
    on('classic.PreModelSwitch', () => ({permissionDecision:decision}));
    await $.session.start(START);
    await $.command.run({command:'model', args:''});
    expect((await $.classic.PreModelSwitch(ENTER_AUTO)).permissionDecision).toBe(decision);
  });
}

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

test('each /model auto main-model save updates the hint without logging', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS);
  const sent = captureSteps(on);
  const rows = captureNotices(on);
  const hint = await hintTail($, on);
  await $.session.start(START);

  expect(await hint()).toBe('Auto (Sonnet 5) · smart router ON');
  for (const [model, label] of [[OTHER, 'Haiku 4.5'], [MAIN, 'Sonnet 5']] as const) {
    rows.length = 0;
    await $.command.run({ command: 'model', args: 'auto' });
    expect(rows).toEqual([]);
    session.commands.length = 0;
    await session.choose(model);
    expect(session.commands.filter(c => c.command === 'model' || c.command === 'effort')).toEqual([]);
    expect(session.env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe(DEFAULT_ALIAS);
    expect(session.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    expect(rows).toEqual([]);
    expect(session.selected()).toBe(DEFAULT_ALIAS);
    expect(await hint()).toBe(`Auto (${label}) · smart router ON`);
    sent.length = 0;
    await step($, DEFAULT_ALIAS);
    await step($, DEFAULT_ALIAS, 'child');
    expect(sent).toEqual([model, model]);
    expect(session.store[AUTO.storeKey]).toBe(model);
  }
});

test('ending a session dismisses the chooser and keeps the model for the next launch', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS, OTHER);
  const rows = captureNotices(on);
  await $.session.start(START);
  await $.command.run({command:'model', args:'auto'});
  await $.session.end({reason:'prompt_input_exit'});
  expect(session.closed).toContain('auto-models');
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  expect(rows).toEqual([]);

  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  await session.choose(MAIN);
  expect(session.store[AUTO.storeKey]).toBe(MAIN);
});

test('opening /model without a pick leaves the chooser closed', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({ command: 'model', args: '' });
  await session.clock.advance(0);
  const picker = await session.picker();
  expect(await picker.find({type:'Select'})).toBeUndefined();
  await picker.unmount();
});

test('a direct non-Auto model command never opens the chooser', { options: OPTIONS }, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({ command: 'model', args: 'system.ai.claude-opus-5' });
  await session.clock.advance(0);
  expect(session.opened).not.toContain('auto-models');
});

for (const stage of ['model lookup', 'pane open', 'deferred focus', 'selection'] as const) {
  test(`the chooser can be retried after ${stage} fails`, {options:OPTIONS}, async ($, on) => {
    const session = staticCatalogSession($, on, DEFAULT_ALIAS, MAIN);
    await $.session.start(START);
    const fail = async () => { throw new Error('native chooser failure'); };
    if (stage === 'model lookup') session.chooser.beforeModel = fail;
    if (stage === 'pane open') session.chooser.beforeOpen = fail;
    await $.command.run({command:'auto', args:''});
    if (stage === 'deferred focus') {
      session.chooser.beforeOpen = fail;
      await session.clock.advance(0);
    }
    if (stage === 'selection') {
      session.chooser.beforeModel = fail;
      await session.choose(OTHER);
    }
    session.chooser.beforeModel = undefined;
    session.chooser.beforeOpen = undefined;
    expect(session.toasts.at(-1)).toContain('Try /model auto');
    expect(session.closed).toContain('auto-models');
    expect(session.store[AUTO.storeKey]).toBe(MAIN);
    await $.command.run({command:'auto', args:''});
    await session.choose(OTHER);
    expect(session.store[AUTO.storeKey]).toBe(OTHER);
  });
}

test('a failed deferred refocus cannot close a newer chooser', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS, MAIN);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  let release: () => void;
  let started: () => void;
  const ready = new Promise<void>(resolve => { started = resolve; });
  session.chooser.beforeOpen = async () => {
    session.chooser.beforeOpen = undefined;
    await new Promise<void>(resolve => { release = resolve; started(); });
    throw new Error('old refocus failed');
  };
  const refocus = session.clock.advance(0);
  await ready;
  await $.command.run({command:'model', args:'auto'});
  const closeCount = session.closed.length;
  release!();
  await refocus;
  expect(session.toasts).toEqual([]);
  expect(session.closed.length).toBe(closeCount);
  await session.choose(OTHER);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
});

test('a model change while the chooser is open explains how to retry', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS, MAIN);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.setSelected(OTHER);
  await session.choose(OTHER);
  expect(session.toasts.at(-1)).toContain('The model changed');
  expect(session.store[AUTO.storeKey]).toBe(MAIN);
  expect(session.closed).toContain('auto-models');
});

test('a failed preference write keeps the chooser seat until a successful retry', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS, MAIN);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  session.chooser.beforeStore = () => { throw new Error('write denied'); };
  await session.choose(OTHER);
  expect(session.store[AUTO.storeKey]).toBe(MAIN);
  expect(session.closed).not.toContain('auto-models');
  session.chooser.beforeStore = undefined;
  await session.choose(OTHER);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  expect(session.closed).toContain('auto-models');
});

async function appendNativeNotice($: any, text: string) {
  try {
    await $.session.append({
      door:'command', origin:{kind:'engine'}, uuid:'notice',
      message:{type:'system',content:[{type:'text',text:`<local-command-stdout>${text}</local-command-stdout>`}]},
    });
    return true;
  } catch (error) {
    // 2.1.290 has no transcript sink. It still runs the hook up to next(e).
    if (!String(error).includes('no implementation for session.append')) throw error;
    return false;
  }
}

for (const accepted of [false, true]) {
  test(`a per-model Auto switch ${accepted ? 'saves the accepted' : 'restores the kept'} alias`, {
    options:{...OPTIONS, autoPicker:JSON.stringify({...AUTO, staticModel:undefined})},
  }, async ($, on) => {
    on('command.run', {command:'model'}, () => {
      if (accepted) session.setSelected(OTHER_ALIAS);
      return {text:'Auto mode: Subagents smart routed.'};
    });
    const session = staticCatalogSession($, on, DEFAULT_ALIAS, MAIN);
    await $.session.start(START);
    await $.command.run({command:'auto', args:''});
    await session.choose(OTHER);
    expect(session.env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe(accepted ? OTHER_ALIAS : DEFAULT_ALIAS);
    expect(session.store[AUTO.storeKey]).toBe(accepted ? OTHER : MAIN);
  });
}

test('a manual model confirmation disables routing when closing either pane fails', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS);
  const messages: string[] = [];
  on('session.append', {door:'command'}, ($, e, next) => {
    messages.push(e.message.content[0].text);
    return next(e);
  });
  await $.session.start(START);
  await $.command.run({command:'model', args:MAIN});
  session.chooser.beforeClose = () => { throw new Error('pane already dismissed'); };
  const text = `Set model to \`${MAIN}\``;
  await appendNativeNotice($, text);
  expect(messages).toEqual([`<local-command-stdout>${text}</local-command-stdout>`]);
  expect(session.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
});

test('a native request to pick again leaves the reservation for the next pick', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS, MAIN);
  await $.session.start(START);
  await $.command.run({command:'model', args:''});
  const hasTranscript = await appendNativeNotice($, 'The model changed while you were confirming; pick again');
  expect(session.closed).not.toContain('auto-models');
  if (hasTranscript) {
    await appendNativeNotice($, `Set model to \`${DEFAULT_ALIAS}\``);
    await session.choose(OTHER);
    expect(session.store[AUTO.storeKey]).toBe(OTHER);
  }
});

const LARGE_AUTO = {
  ...AUTO,
  models: Array.from({length:12}, (_, index) => index < 2 ? AUTO.models[index] : {
    model:`system.ai.model-${index}`, label:`Model ${index}`, autoModel:`ug-auto-model-${index}`,
  }),
};

test('one Select includes the entire catalog and saves the last model', {
  options:{...OPTIONS, autoPicker:JSON.stringify(LARGE_AUTO)},
}, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS, OTHER);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  const picker = await session.picker();
  const select = await picker.find({type:'Select'});
  expect(select.props.options.map((row: {label:string}) => row.label))
    .toEqual(['Haiku 4.5', 'Sonnet 5', ...Array.from({length:10}, (_, index) => `Model ${index + 2}`)]);
  await picker.select({key:select.props.key, value:'system.ai.model-11'});
  expect(session.store[AUTO.storeKey]).toBe('system.ai.model-11');
  await picker.unmount();
});

test('models with identical labels can be selected by their distinct IDs', {
  options:{...OPTIONS, autoPicker:JSON.stringify({...AUTO, models:AUTO.models.map(row => ({...row, label:'Shared'}))})},
}, async ($, on) => {
  const session = staticCatalogSession($, on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  const picker = await session.picker();
  const select = await picker.find({type:'Select'});
  expect(select.props.options).toEqual([
    {value:MAIN, label:`Shared (${MAIN})`}, {value:OTHER, label:`Shared (${OTHER})`},
  ]);
  await picker.select({key:select.props.key, value:OTHER});
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  await picker.unmount();
});

for (const interrupt of ['model', 'end'] as const) {
  test(`a selection overtaken by ${interrupt} cannot save a stale model`, {options:OPTIONS}, async ($, on) => {
    const session = staticCatalogSession($, on, DEFAULT_ALIAS, MAIN);
    let release: () => void;
    let started: () => void;
    const ready = new Promise<void>(resolve => { started = resolve; });
    await $.session.start(START);
    await $.command.run({command:'auto', args:''});
    session.chooser.beforeModel = async () => {
      session.chooser.beforeModel = undefined;
      await new Promise<void>(resolve => { release = resolve; started(); });
    };
    const saving = session.choose(OTHER);
    await ready;
    session.chooser.beforeClose = () => { throw new Error('pane already dismissed'); };
    if (interrupt === 'model') await $.command.run({command:'model', args:MAIN});
    else await $.session.end({reason:'prompt_input_exit'});
    release!();
    await saving;
    expect(session.store[AUTO.storeKey]).toBe(MAIN);
  });
}

test('direct /model auto keeps the entry model without a cache warning', {options:OPTIONS}, async ($, on) => {
  const session = staticCatalogSession($, on, OTHER, undefined, 1000);
  const rows = captureNotices(on);
  let warned = false;
  on('tool.call', {tool:'AskUserQuestion'}, ($, e) => {
    warned = true;
    return {result:{answers:{}}};
  });
  await $.session.start(START);
  await $.command.run({command:'model', args:'auto'});
  await session.choose(OTHER);
  expect(warned).toBe(false);
  expect(session.store[AUTO.storeKey]).toBe(OTHER);
  expect(session.opened).toContain('auto-models');
  expect(rows).toEqual([]);
});
