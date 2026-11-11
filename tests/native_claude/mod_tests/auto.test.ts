import { expect, mock, test } from 'claude-code/testing';
import { register } from '../hooks/register.ts';

const config = {
  model: 'ug-auto-opus', description: 'Auto description', mainModel: 'opus', storeKey: 'workspace-main',
  models: [
    {model: 'opus', label: 'Opus 5.5', autoModel: 'ug-auto-opus'}, {model: 'fable-old', label: 'Fable 5.0', autoModel: 'ug-auto-fable-old'},
    {model: 'fable', label: 'Fable 5.1', autoModel: 'ug-auto-fable'}, {model: 'astra', label: 'Astra 6.0', autoModel: 'ug-auto-astra'},
  ],
};
const options = {
  ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '0', unset: [], recipe: 'task_v3',
  autoPicker: JSON.stringify(config),
};
const start = {surface: 'terminal', isInteractive: true, cwd: '/work'} as const;

function host(on: any, selected = 'ug-auto-opus') {
  const state = {selected, modelResult: {text: 'Set model'} as any, saved: undefined as unknown, ask: 'Astra 6.0', questions: [] as any[], closed: [] as any[], appendClosed: [] as number[], appended: [] as string[], invalidations: [] as string[], calls: [] as any[], forwarded: [] as any[], tails: [] as any[]};
  const env: Record<string, string | undefined> = {
    ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: '0', UCODE_SMART_ROUTER_PYTHON: '/ug/python',
    UCODE_SESSION_ENV_FILE: '/session/env.json',
  };
  const clock = mock.clock(on);
  on('env.get', ($: any, e: any) => ({value: env[e.name]}));
  on('env.set', ($: any, e: any) => {env[e.name] = e.value; return {value: undefined};});
  on('fs.read', () => ({value: '{}'}));
  on('command.run', {command: 'model'}, ($: any, e: any, next: any) => {
    if (!e.args) return next(e);
    state.selected = e.args; return state.modelResult;
  });
  on('settings.read', () => ({value:{}}));
  on('session.model', () => ({value: state.selected}));
  on('session.start', () => ({cwd: '/work'}));
  on('ui.invalidate', () => ({value: undefined}));
  on('command.register', () => ({value: {}}));
  on('store.get', () => ({value: state.saved}));
  on('store.set', ($: any, e: any) => {state.saved = e.value; return {value: undefined};});
  on('process.run', ($: any, e: any) => {state.calls.push(e); return {value: {exitCode: 0, stdout: '', stderr: ''}};});
  on('ui.open', ($: any, e: any) => { state.questions.push(e); return {value: {isPlaced: true}}; });
  on('ui.close', ($: any, e: any) => {state.closed.push(e); return {value: undefined};});
  on('agent.list', () => ({value: []}));
  on('ui.toast', () => ({value: undefined}));
  on('ui.render', {component: 'PromptHint'}, ($: any, e: any) => {
    state.tails.push(e.props.tail);
    return $.ui.resolve(e).Text({children: [e.props.tail ?? '']});
  });
  on('turn.step', async function* ($: any, e: any) {
    state.forwarded.push(e);
    return {turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null};
  });
  return {state, env, clock};
}

// Claude 2.1.290 has no mock.session host implementation. Exercise the real
// append middleware with an explicit continuation; the terminal integration
// journey covers native transcript dispatch as well as the resulting UI.
function commandTranscript({state, env}: ReturnType<typeof host>) {
  let append: any;
  register(((event: string, ...args: any[]) => {
    if (event === 'session.append' && args[0]?.door === 'command') append = args.at(-1);
  }) as any, options);
  return async (text: string) => {
    const pending: Array<() => Promise<void>> = [];
    const engine = {
      session: {model: async () => state.selected},
      env: {
        get: async (key: string) => env[key],
        set: async (key: string, value: string) => {env[key] = value;},
      },
      fs: {read: async () => '{}'},
      process: {run: async (args: any) => {
        state.calls.push(args);
        return {exitCode: 0, stdout: '', stderr: ''};
      }},
      ui: {
        open: async (request: any) => {state.questions.push(request);},
        close: async (request: any) => {state.closed.push(request);},
        invalidate: (event: string) => {state.invalidations.push(event);},
      },
      clock: {after: (_delay: number, callback: () => Promise<void>) => {
        let cancelled = false;
        pending.push(async () => {if (!cancelled) await callback();});
        return {cancel: () => {cancelled = true;}};
      }},
    };
    const event = {door: 'command', uuid: text, message: {type: 'user', content: [
      {type: 'text', text: `<local-command-stdout>${text}</local-command-stdout>`},
    ]}};
    await append(engine, event, async (forwarded: any) => {
      state.appendClosed.push(state.closed.length);
      state.appended.push(forwarded.message.content[0].text);
      return {message: forwarded.message, uuid: forwarded.uuid};
    });
    for (const callback of pending) await callback();
  };
}

async function step($: any, agentId?: string) {
  const stream = $.turn.step({turnId: 'turn-1', index: 1, model: 'native', messageCount: 1, agentId});
  while (!(await stream.next()).done) {}
}

async function hint($: any, state: any) {
  const mounted = await $.ui.mount({plugin: 'ug-smart-router', component: 'PromptHint', surface: 'terminal', props: {tail: 'native'}});
  await mounted.unmount();
  return state.tails.at(-1);
}

test('Auto sends the main model and retains the native model display with a separate indicator', {options}, async ($, on) => {
  const {state} = host(on);
  await $.session.start(start);
  await step($);
  expect(state.forwarded[0].model).toBe('opus');
  expect(state.selected).toBe('ug-auto-opus');
  expect(await hint($, state)).toBe('native · Auto (Opus 5.5)');
  await step($, 'child');
  expect(state.forwarded[1].model).toBe('native');
});

test('/auto changes only the main preference and offers the complete catalog', {options}, async ($, on) => {
  const {state, env} = host(on, 'opus');
  await $.session.start(start);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  await $.command.run({command: 'auto'});
  expect(state.questions[0].id).toBe('auto-main');
  const pane = await $.ui.mount({plugin: 'ug-smart-router', component: 'Pane', requestId: 'auto-main', surface: 'terminal', props: {} as any});
  await pane.press({key:'auto-main-model-3'});
  await pane.unmount();
  expect(state.saved).toBe('astra');
  expect(state.selected).toBe('opus');
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(await hint($, state)).toBe('native');
  state.selected = env.ANTHROPIC_CUSTOM_MODEL_OPTION!;
  await step($);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(state.forwarded[0].model).toBe('astra');
  expect(await hint($, state)).toBe('native · Auto (Astra 6.0)');
});

test('cancellation and a removed saved model preserve a valid default', {options}, async ($, on) => {
  const {state} = host(on);
  state.saved = 'removed';
  state.ask = 'dismiss';
  await $.command.run({command: 'auto'});
  expect(state.saved).toBe('removed');
  await step($);
  expect(state.forwarded[0].model).toBe('opus');
});

test('reselecting Auto opens the next step and manual selection disables routing', {options}, async ($, on) => {
  const context = host(on);
  const {state, env} = context;
  const confirm = commandTranscript(context);
  const select = async (label: string) => {
    await confirm(`Set model to \`${label}\``);
  };
  await select('Auto');
  expect(state.saved).toBeUndefined();
  expect(state.questions.length).toBe(1);
  state.selected = 'opus';
  await select('Opus');
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(state.questions.length).toBe(1);
  expect(await hint($, state)).toBe('native');
});

test('Auto confirmations preserve native save details and warnings', {options}, async ($, on) => {
  const context = host(on);
  const confirm = commandTranscript(context);
  const details = ' and saved as your default for new sessions with `high` effort\nA native warning';
  await confirm('Set model to `Opus 5.5`' + details);
  expect(context.state.appended.at(-1)).toBe('<local-command-stdout>Set model to `Auto`' + details + '</local-command-stdout>');
  expect(context.state.selected).toBe('ug-auto-opus');
  context.state.selected = 'opus';
  await confirm('Set model to `Opus 5.5`' + details);
  expect(context.state.appended.at(-1)).toBe('<local-command-stdout>Set model to `Opus 5.5`' + details + '</local-command-stdout>');
  context.state.selected = 'ug-auto-opus';
  await confirm('Kept model as `Opus 5.5`');
  expect(context.state.appended.at(-1)).toBe('<local-command-stdout>Kept model as `Opus 5.5`</local-command-stdout>');
});

test('native model panel places Auto before its transcript and dismisses it on cancellation', {options}, async ($, on) => {
  const context = host(on);
  const {state} = context;
  on('command.run', {command: 'model'}, () => ({ref: 1}));
  const confirm = commandTranscript(context);
  await $.command.run({command: 'model', args: ''});
  expect(state.questions.length).toBe(1);
  expect(state.questions[0].id).toBe('auto-main');
  await confirm('Set model to Auto for this session only');
  expect(state.questions.length).toBe(2);
  expect(state.closed.length).toBe(0);
  await $.command.run({command: 'model', args: ''});
  await confirm('Kept model as Auto');
  expect(state.closed[0].id).toBe('auto-main');
  state.selected = 'opus';
  await $.command.run({command: 'model', args: ''});
  await confirm('Set model to Opus');
  expect(state.questions.length).toBe(4);
  expect(state.closed.length).toBe(2);
});

test('direct model commands open the main selector only for Auto', {options}, async ($, on) => {
  const {state} = host(on, 'opus');
  state.modelResult = {ref: 1};
  await $.command.run({command: 'model', args: 'opus'});
  expect(state.questions.length).toBe(0);
  state.selected = 'ug-auto-opus';
  await $.command.run({command: 'model', args: 'ug-auto-opus'});
  expect(state.questions.length).toBe(1);
  expect(state.questions[0].id).toBe('auto-main');
});


test('/auto switches the native identity while Auto is selected', {options}, async ($, on) => {
  const {state, env} = host(on);
  await $.session.start(start);
  await $.command.run({command: 'auto'});
  const pane = await $.ui.mount({plugin: 'ug-smart-router', component: 'Pane', requestId: 'auto-main', surface: 'terminal', props: {} as any});
  await pane.press({key:'auto-main-model-3'});
  await pane.unmount();
  expect(state.selected).toBe('ug-auto-astra');
  expect(state.saved).toBe('astra');
  expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe('ug-auto-astra');
  expect(state.questions.length).toBe(1);
  await step($);
  expect(state.forwarded[0].model).toBe('astra');
  expect(await hint($, state)).toBe('native · Auto (Astra 6.0)');
  state.saved = 'opus'; // Another session's persisted preference cannot change this profile.
  await step($);
  expect(state.forwarded[1].model).toBe('astra');
  expect(await hint($, state)).toBe('native · Auto (Astra 6.0)');
});


test('manual selection and cancellation close Auto before the native picker confirmation renders', {options}, async ($, on) => {
  const context = host(on);
  const {state, env} = context;
  on('command.run', {command: 'model'}, () => ({ref: 1}));
  const confirm = commandTranscript(context);
  await $.command.run({command: 'model', args: ''});
  state.selected = 'opus';
  await confirm('Set model to Opus');
  expect(state.appendClosed.at(-1)).toBe(1);
  expect(state.invalidations).toContain('ui.render');
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  await $.command.run({command: 'model', args: ''});
  await confirm('Kept model as Opus');
  expect(state.appendClosed.at(-1)).toBe(2);
});

test('Auto arrow scrolling selects a model but wheel and page scrolling remain native', async () => {
  let scroll: any;
  register(((event: string, ...args: any[]) => {
    if (event === 'ui.scroll' && args[0]?.requestId === 'auto-main') scroll = args.at(-1);
  }) as any, options);
  const focused: any[] = [];
  const revealed: any[] = [];
  const forwarded: any[] = [];
  const engine = {ui: {
    focus: async (request: any) => {focused.push(request); return {};},
    scroll: async (request: any) => {revealed.push(request); return {};},
  }};
  const event = {component:'Pane', requestId:'auto-main', offset:1, by:1,
    bodyRows:5, contentRows:12, origin:{kind:'person'}};
  const next = async (event: any) => {forwarded.push(event); return {};};
  await scroll(engine, event, next);
  expect(focused).toEqual([{requestId:'auto-main',key:'auto-main-model-1'}]);
  expect(revealed).toEqual([{in:'auto-main',to:{key:'auto-main-model-1'},block:'nearest'}]);
  expect(forwarded).toHaveLength(0);
  await scroll(engine, {...event,pointer:{row:2,column:1}}, next);
  await scroll(engine, {...event,by:5}, next);
  await scroll(engine, {...event,origin:{kind:'plugin',name:'ug-smart-router'}}, next);
  expect(forwarded).toHaveLength(3);
  expect(focused).toHaveLength(1);
});
