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
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '0', ENABLE_SMART_ROUTER_AUTO_MODE: '1', unset: [], recipe: 'task_v3',
  autoPicker: JSON.stringify(config),
};
const start = {surface: 'terminal', isInteractive: true, cwd: '/work'} as const;

function host(on: any, selected = 'ug-auto-opus') {
  const state = {selected, rejectModel: false, processExitCode: 0, agents: [] as any[], toasts: [] as string[], modelResult: {text: 'Set model'} as any, saved: undefined as unknown, ask: 'Astra 6.0', questions: [] as any[], closed: [] as any[], appendClosed: [] as number[], appended: [] as string[], invalidations: [] as string[], calls: [] as any[], forwarded: [] as any[], tails: [] as any[]};
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
    if (!state.rejectModel) state.selected = e.args; return state.modelResult;
  });
  on('command.run', {command:'effort'}, () => ({text:'Effort level: auto (currently medium)'}));
  on('settings.read', () => ({value:{}}));
  on('session.model', () => ({value: state.selected}));
  on('session.start', () => ({cwd: '/work'}));
  on('command.register', () => ({value: {}}));
  on('store.get', () => ({value: state.saved}));
  on('store.set', ($: any, e: any) => {state.saved = e.value; return {value: undefined};});
  on('process.run', ($: any, e: any) => {state.calls.push(e); return {value: {exitCode: state.processExitCode, stdout: '', stderr: ''}};});
  on('ui.open', ($: any, e: any) => { state.questions.push(e); return {value: {isPlaced: true}}; });
  on('ui.close', ($: any, e: any) => {state.closed.push(e); return {value: undefined};});
  on('agent.list', () => ({value: state.agents}));
  on('ui.toast', ($: any, e: any) => {state.toasts.push(JSON.stringify(e)); return {value: undefined};});
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
  let model: any;
  register(((event: string, ...args: any[]) => {
    if (event === 'session.append' && args[0]?.door === 'command' && !append) append = args.at(-1);
    if (event === 'command.run' && args[0]?.command === 'model' && !model) model = args.at(-1);
  }) as any, options);
  return async (text: string, metadata: {raw?: boolean; origin?: {kind: string; name?: string}; selectAuto?: boolean} = {}) => {
    const pending: Array<() => Promise<void>> = [];
    const engine = {
      session: {model: async () => state.selected},
      command: {run: async () => ({text:'Effort level: auto (currently medium)'})},
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
        focus: async () => ({}),
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
    if (metadata.selectAuto) await model(engine, {command:'model', args:'auto', origin:{kind:'composer'}}, async () => ({}));
    const event = {door: 'command', origin: metadata.origin ?? {kind: 'composer'}, uuid: text, message: {type: 'user', content: [
      {type: 'text', text: metadata.raw ? text : `<local-command-stdout>${text}</local-command-stdout>`},
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
  expect(await hint($, state)).toBe('native · Auto (Opus 5.5) · smart router ON');
  await step($, 'child');
  expect(state.forwarded[1].model).toBe('native');
});

test('Auto bands show successful model changes and hide after the next completed prompt', {options}, async ($, on) => {
  const {state} = host(on);
  on('ui.render', {component:'AbovePrompt'}, ($, e) => $.ui.resolve(e).Text({children:['native content']}));
  on('prompt.submit', ($, e) => ({text:e.text}));
  on('turn.complete', () => ({text:''}));
  await $.session.start(start);
  const band = await $.ui.mount({plugin:'ug-smart-router', component:'AbovePrompt', surface:'terminal', props:{}});
  const shown = async (enabled: boolean) => {
    const tree = JSON.stringify(await band.drawn());
    expect(tree).toContain('unity gateway smart router');
    expect(tree).toContain(enabled ? ' ON ' : ' OFF ');
    expect(tree).toContain(enabled ? 'green' : 'gray');
    expect(tree).toContain('native content');
    expect(tree).toContain(enabled ? 'routing subagents' : "Reenable with Auto in '/model'");
    expect(await hint($, state)).toContain(enabled ? 'smart router ON' : 'smart router OFF');
  };
  const complete = async () => {
    await $.prompt.submit({text:'next prompt'});
    expect(JSON.stringify(await band.drawn())).toContain('unity gateway smart router');
    await $.turn.complete({answer:'done'});
    expect(JSON.stringify(await band.drawn())).not.toContain('unity gateway smart router');
    await $.prompt.submit({text:'second prompt'});
    await step($);
    await hint($, state);
    expect(JSON.stringify(await band.drawn())).not.toContain('unity gateway smart router');
    await $.turn.complete({answer:'done'});
    expect(JSON.stringify(await band.drawn())).not.toContain('unity gateway smart router');
  };
  await shown(true);
  state.agents = [{id:'child', status:'running'}];
  expect(await hint($, state)).toBe('native · Auto (Opus 5.5) · smart router ON · 1 subagent running');
  state.agents = [];
  await complete();
  await $.command.run({command:'auto'});
  const pane = await $.ui.mount({plugin:'ug-smart-router', component:'Pane', requestId:'auto-main', surface:'terminal', props:{} as any});
  await pane.press({key:'auto-main-model-3'});
  await pane.unmount();
  expect(state.selected).toBe('ug-auto-astra');
  await shown(true);
  await complete();
  state.selected = 'opus';
  await step($);
  await shown(false);
  await complete();
  state.selected = 'ug-auto-astra';
  await step($);
  await shown(true);
  await complete();
  state.selected = 'opus';
  state.processExitCode = 1;
  await step($);
  expect(state.toasts.at(-1)).toContain('Could not update Smart Routing');
  expect(JSON.stringify(await band.drawn())).not.toContain('unity gateway smart router');
  await band.unmount();
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
  expect(await hint($, state)).toBe('native · smart router OFF');
  state.selected = env.ANTHROPIC_CUSTOM_MODEL_OPTION!;
  await step($);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(state.forwarded[0].model).toBe('astra');
  expect(await hint($, state)).toBe('native · Auto (Astra 6.0) · smart router ON');
});

test('/model auto selects the configured Auto profile and opens its controls', {options}, async ($, on) => {
  const {state, env} = host(on, 'opus');
  state.saved = 'astra';
  await $.session.start(start);
  await $.command.run({command:'model', args:'auto'});
  expect(state.selected).toBe('ug-auto-astra');
  expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe('ug-auto-astra');
  expect(state.saved).toBe('astra');
  expect(state.questions.length).toBe(1);
  expect(state.questions[0].id).toBe('auto-main');
  await step($);
  expect(state.forwarded[0].model).toBe('astra');
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

test('Auto help exposes /model auto and hides the internal /auto command', {options}, async ($, on) => {
  host(on);
  on('command.describe', ($: any, e: any) => ({description:e.description, isHidden:e.isHidden}));
  const description = await $.command.describe({command:'auto', description:'Customize main agent model',
    isHidden:false, immediate:true, provider:{plugin:'ug-smart-router', tier:'user'}});
  expect(description.isHidden).toBe(true);
  const reply = await $.command.run({command:'smart-router', args:'on'});
  expect(reply.text).toContain('Use /model auto');
  expect(reply.text).not.toContain('Use /auto');
});

test('a removed saved model preserves a valid default', {options}, async ($, on) => {
  const {state} = host(on);
  state.saved = 'removed';
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
  expect(await hint($, state)).toBe('native · smart router OFF');
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
  expect(context.state.appended.at(-1)).toBe('<local-command-stdout>Kept model as `Auto`</local-command-stdout>');
});

test('only UG internal model-command echoes relabel configured Auto aliases', {options}, async ($, on) => {
  const context = host(on);
  const append = commandTranscript(context);
  const echo = '<command-name>/model</command-name>\n            <command-message>model</command-message>\n            <command-args>ug-auto-opus</command-args>';
  const metadata = {raw:true, origin:{kind:'plugin', name:'ug-smart-router'}};
  await append(echo, metadata);
  expect(context.state.appended.at(-1)).toBe(echo.replace('ug-auto-opus', 'Auto'));
  expect(context.state.selected).toBe('ug-auto-opus');
  expect(context.state.saved).toBeUndefined();
  expect(context.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  for (const origin of [{kind:'composer'}, {kind:'plugin', name:'another-plugin'}]) {
    await append(echo, {raw:true, origin});
    expect(context.state.appended.at(-1)).toBe(echo);
  }
  for (const text of [
    echo.replace('ug-auto-opus', 'ug-auto-unconfigured'),
    echo.replace('ug-auto-opus', 'opus'),
    'Discuss ug-auto-opus in this example',
    echo + '\nDiscuss ug-auto-opus',
  ]) {
    await append(text, metadata);
    expect(context.state.appended.at(-1)).toBe(text);
  }
});

for (const kind of ['composer', 'unclassified']) {
  test(`a foreign echo cannot consume a pending ${kind} /model auto echo`, {options}, async ($, on) => {
    const context = host(on);
    const append = commandTranscript(context);
    const echo = '<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args>ug-auto-opus</command-args>';
    await append(echo, {raw:true, selectAuto:true, origin:{kind:'plugin', name:'another-plugin'}});
    expect(context.state.appended.at(-1)).toBe(echo);
    await append(echo, {raw:true, origin:{kind}});
    expect(context.state.appended.at(-1)).toBe(echo.replace('ug-auto-opus', 'auto'));
    await append(echo, {raw:true, origin:{kind}});
    expect(context.state.appended.at(-1)).toBe(echo);
  });
}

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
  expect(await hint($, state)).toBe('native · Auto (Astra 6.0) · smart router ON');
  state.saved = 'opus'; // Another session's persisted preference cannot change this profile.
  await step($);
  expect(state.forwarded[1].model).toBe('astra');
  expect(await hint($, state)).toBe('native · Auto (Astra 6.0) · smart router ON');
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


test('Auto remains unavailable when the launch feature flag is disabled', {options:{...options, ENABLE_SMART_ROUTER_AUTO_MODE:'0'}}, async ($, on) => {
  const {state, env} = host(on, 'opus');
  await $.session.start(start);
  expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBeUndefined();
  expect(state.questions).toHaveLength(0);
  expect(await hint($, state)).toBe('native · smart router ON');
});

test('a rejected main-model switch preserves the current selection and preference', {options}, async ($, on) => {
  const {state, env} = host(on);
  state.rejectModel = true;
  state.modelResult = {text:'Model not permitted'};
  await $.session.start(start);
  await $.command.run({command:'auto'});
  const pane = await $.ui.mount({plugin:'ug-smart-router', component:'Pane', requestId:'auto-main', surface:'terminal', props:{} as any});
  await pane.press({key:'auto-main-model-3'});
  await pane.unmount();
  expect(state.selected).toBe('ug-auto-opus');
  expect(state.saved).toBeUndefined();
  expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe('ug-auto-opus');
});

for (const selected of ['opus', 'ug-auto-opus']) {
  test(`failed routing controls fail closed when switching to ${selected}`,
    {options: {...options, routeCommand: ['/ug/router']}}, async ($, on) => {
      const {state} = host(on, selected === 'opus' ? 'ug-auto-opus' : 'opus');
      on('tool.call', () => ({result: 'native child'}));
      await $.session.start(start);
      state.selected = selected;
      state.processExitCode = 1;
      const before = state.calls.length;
      await step($);
      expect(state.calls.length).toBe(before + 1);
      const child = await $.tool.call({tool:'Agent', tool_use_id:'child', prompt:'Calculate 2+2', description:'Calculate', subagent_type:'general-purpose'});
      expect(child.result).toBe('native child');
      expect(state.calls.length).toBe(before + 1);
      await step($);
      expect(state.calls.length).toBe(before + 1);
    });
}

test('a failed routing sync warns once and main turns continue', {options}, async ($, on) => {
  const {state, env} = host(on);
  env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY = '0';
  state.processExitCode = 1;
  await step($);
  await step($);
  expect(state.forwarded.length).toBe(2);
  expect(state.toasts.length).toBe(1);
  expect(state.toasts[0]).toContain('Could not update Smart Routing');
});

test('rejected main model leaves the pane open and shows a retry message', {options}, async ($, on) => {
  const {state} = host(on);
  state.rejectModel = true;
  await $.command.run({command:'auto'});
  const pane = await $.ui.mount({plugin:'ug-smart-router', component:'Pane', requestId:'auto-main', surface:'terminal', props:{} as any});
  await pane.press({key:'auto-main-model-3'});
  expect(state.selected).toBe('ug-auto-opus');
  expect(state.saved).toBeUndefined();
  expect(state.closed.length).toBe(0);
  expect(state.toasts.at(-1)).toContain('Could not save Auto');
  await pane.unmount();
});

test('manual models still show the running agent count', {options}, async ($, on) => {
  const {state} = host(on, 'opus');
  await $.session.start(start);
  state.agents = [{id:'a',status:'running'}];
  expect(await hint($,state)).toBe('native · smart router OFF · 1 subagent running');
});

const feedbackOptions = {...options, isaacExecutable:'/isaac/bin/isaac', isaacFeedbackDirectory:'/isaac/feedback'};

// The pinned native test host has no session.append implementation. Dispatch its
// real middleware and feedback form against controlled engine boundaries.
async function manualFeedbackFlow() {
  const commands: Record<string, any[]> = {};
  const appendHandlers: any[] = [];
  const append = async (engine: any, event: any, next: any) => {
    const invoke = async (index: number, e: any): Promise<any> => index < appendHandlers.length
      ? appendHandlers[index](engine, e, (forwarded: any) => invoke(index + 1, forwarded)) : next(e);
    return invoke(0, event);
  };
  const startHandlers: any[] = [];
  let endHandler: any;
  const closeHandlers: Array<{id?: string; handler: any}> = [];
  let promptHandler: any;
  let turnHandler: any;
  const renders: any[] = [];
  register(((event: string, ...args: any[]) => {
    const handler = args.at(-1);
    if (event === 'command.run') (commands[args[0].command] ??= []).push(handler);
    if (event === 'session.append' && args[0]?.door === 'command') appendHandlers.push(handler);
    if (event === 'session.start') startHandlers.push(handler);
    if (event === 'session.end' && !endHandler) endHandler = handler;
    if (event === 'ui.close') closeHandlers.push({id:args[0]?.id, handler});
    if (event === 'session.append' && args[0]?.door === 'prompt' && !promptHandler) promptHandler = handler;
    if (event === 'turn.step' && typeof args[0] === 'function') turnHandler = handler;
    if (event === 'ui.render' && args[0]?.component === 'Pane' && !args[0]?.requestId) renders.push(handler);
  }) as any, feedbackOptions);
  const state = {
    model:'ug-auto-astra', calls:[] as any[], panes:[] as string[], logs:[] as string[], toasts:[] as string[],
    rejectManual:false, rejectRestore:false, failOff:false, failPrepare:false, failUpload:false, deferConfirmations:false,
    controls:{} as Record<string,string>, reason:undefined as any, cancel:undefined as any,
    offWrite:undefined as undefined | (() => Promise<void>), nativeModels:[] as string[],
  };
  const env: Record<string,string | undefined> = {
    ...options, ISAAC_SMART_ROUTER_FEEDBACK:'1', UCODE_SMART_ROUTER_PYTHON:'/ug/python',
    UCODE_SESSION_ENV_FILE:'/session/env.json',
  };
  const pending: Array<() => Promise<void>> = [];
  const engine: any = {
    session:{model:async () => state.model, id:async () => 'session-1'},
    store:{get:async () => undefined},
    env:{get:async (key: string) => env[key], set:async (key: string, value: string) => {env[key] = value;}},
    fs:{read:async () => JSON.stringify(state.controls), exists:async () => false},
    command:{register:async () => ({}), run:async (e: any) => {
      const handlers = commands[e.command] ?? [];
      const invoke = async (index: number, event: any): Promise<any> => {
        if (index < handlers.length) return handlers[index](engine, event, (nextEvent: any) => invoke(index + 1, nextEvent));
        if (event.command === 'model' && event.args) {
          state.nativeModels.push(event.args);
          if (!(state.rejectManual && event.args === 'opus') && !(state.rejectRestore && event.args === 'ug-auto-astra')) state.model = event.args;
          const confirmation = async () => append(engine, {door:'command', origin:{kind:'plugin',name:'ug-smart-router'}, message:{content:[{type:'text',text:'<local-command-stdout>Set model to `model`</local-command-stdout>'}]}}, async () => ({}));
          if (state.deferConfirmations) pending.push(confirmation);
          else await confirmation();
        }
        return {text:''};
      };
      return invoke(0, {...e,origin:{kind:'plugin',name:'ug-smart-router'}});
    }},
    process:{run:async (argv: string[], processOptions: any) => {
      state.calls.push({argv,...processOptions});
      if (argv.includes('--inspect-target')) return {exitCode:state.failPrepare ? 1 : 0,stdout:'{"message_id":"m1"}',stderr:''};
      const off = argv.includes('--disable-smart-routing');
      if (off && state.offWrite) await state.offWrite();
      const exitCode = (off && state.failOff) || (argv.includes('--with-evidence') && state.failUpload) ? 1 : 0;
      if (!exitCode && (off || argv.includes('--enable-smart-routing'))) state.controls = {ENABLE_SMART_ROUTING_SUBAGENT_ONLY:off ? '0' : '1'};
      return {exitCode,stdout:'',stderr:exitCode ? 'controlled failure' : ''};
    }},
    ui:{
      invalidate:() => {}, open:async (e: any) => {state.panes.push(e.id); return {isPlaced:true};},
      close:async (e: any) => {
        for (const {id, handler} of closeHandlers) {
          if (!id || id === e.id) await handler(engine, e, async () => ({}));
        }
        return {};
      },
      toast:(text: string) => {state.toasts.push(text);}, log:(text: string) => {state.logs.push(text);},
      resolve:() => ({Box:(props: any) => props, Text:(props: any) => props,
        Input:(props: any) => {state.reason = props.onSubmit; return props;},
        Button:(props: any) => {if (props.key === 'cancel') state.cancel = props.onPress; return props;}}),
    },
    clock:{after:(_delay: number, callback: () => Promise<void>) => {
      let cancelled = false;
      pending.push(async () => {if (!cancelled) await callback();});
      return {cancel:() => {cancelled = true;}};
    }},
  };
  const flush = async () => {while (pending.length) await pending.shift()!();};
  const select = async (model: string) => {
    await engine.command.run({command:'model',args:''});
    state.model = model;
    await append(engine, {door:'command',origin:{kind:'composer'},message:{content:[{type:'text',text:'<local-command-stdout>Set model to `model`</local-command-stdout>'}]}}, async () => ({}));
    await flush();
  };
  const render = async () => {
    for (const handler of renders) await handler(engine, {requestId:'auto-main',surface:'terminal'}, async () => ({}));
  };
  for (const handler of startHandlers) await handler(engine, start, async () => ({}));
  const reconcile = async () => {
    const stream = turnHandler(engine, {}, async function* () {return {};});
    while (!(await stream.next()).done) {}
  };
  return {state,env,select,render,run:engine.command.run,end:async () => endHandler(engine, {}, async () => ({})),
    reconcile, flush,
    prompt:async () => promptHandler(engine, {message:{}}, async () => ({})),
    offCalls:() => state.calls.filter(call => call.argv.includes('--disable-smart-routing'))};
}

test('feedback-gated native manual selection restores the exact Auto until a valid reason commits', async () => {
  const flow = await manualFeedbackFlow();
  await flow.select('opus');
  expect(flow.state.model).toBe('ug-auto-astra');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(flow.state.panes).toEqual(['auto-main']);
  expect(flow.offCalls()).toEqual([]);
  await flow.render();
  await flow.state.reason('too short');
  expect(flow.state.toasts.at(-1)).toContain('at least 16');
  expect(flow.offCalls()).toEqual([]);
  const reason = 'The routed model was too slow';
  await flow.state.reason(reason);
  expect(flow.offCalls().map(call => call.argv)).toEqual([
    ['/ug/python','-m','ucode.cli','claude','--disable-smart-routing','--smart-router-feedback',reason],
  ]);
  expect(flow.state.model).toBe('opus');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(flow.state.controls.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(flow.state.logs).toContain('Smart Router is off');
  await flow.select('ug-auto-fable');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(flow.state.controls.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

test('cancelling feedback leaves the restored Auto and routing on', async () => {
  const flow = await manualFeedbackFlow();
  await flow.select('opus');
  await flow.render();
  await flow.state.cancel();
  expect(flow.state.model).toBe('ug-auto-astra');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(flow.offCalls()).toEqual([]);
  flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY = '0';
  await flow.reconcile();
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

for (const failure of ['failOff','rejectManual','failPrepare'] as const) {
  test(`feedback manual switch preserves Auto ON after ${failure}`, async () => {
    const flow = await manualFeedbackFlow();
    flow.state[failure] = true;
    await flow.select('opus');
    if (failure !== 'failPrepare') {
      await flow.render();
      await flow.state.reason('The routed model was too slow');
      expect(flow.state.logs).not.toContain('Smart Router is off');
    } else {
      expect(flow.state.panes).toEqual([]);
      expect(flow.offCalls()).toEqual([]);
      expect(flow.state.toasts.at(-1)).toContain('Could not open the feedback form');
    }
    expect(flow.state.model).toBe('ug-auto-astra');
    expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    expect(flow.state.controls.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).not.toBe('0');
  });
}

for (const newer of ['Auto','manual','session end','prompt']) {
  test(`a stale feedback callback cannot disable routing after ${newer}`, async () => {
    const flow = await manualFeedbackFlow();
    await flow.select('opus');
    await flow.render();
    const submission = flow.state.reason('The routed model was too slow');
    if (newer === 'session end') await flow.end();
    else if (newer === 'prompt') await flow.prompt();
    else await flow.select(newer === 'Auto' ? 'ug-auto-fable' : 'fable');
    const current = flow.state.model;
    await submission;
    expect(flow.state.model).toBe(current);
    expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    expect(flow.offCalls()).toEqual([]);
    expect(flow.state.logs).not.toContain('Smart Router is off');
  });
}

test('an Auto feedback request cannot admit a public off command or a bypass flag', async () => {
  const flow = await manualFeedbackFlow();
  await flow.select('opus');
  for (const args of ['off','off --feedback-collected']) {
    expect((await flow.run({command:'smart-router',args})).text).toContain('Choose Auto in /model');
  }
  expect(flow.offCalls()).toEqual([]);
});

test('deferred native internal confirmations cannot reopen Auto or start another feedback flow', async () => {
  const flow = await manualFeedbackFlow();
  flow.state.deferConfirmations = true;
  await flow.select('opus');
  expect(flow.state.model).toBe('ug-auto-astra');
  expect(flow.state.panes).toEqual(['auto-main']);
  await flow.render();
  await flow.state.reason('The routed model was too slow');
  await flow.flush();
  expect(flow.state.model).toBe('opus');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(flow.state.panes).toEqual(['auto-main']);
  expect(flow.offCalls()).toHaveLength(1);
});

for (const newer of ['prompt','session end','Auto']) {
  test(`a CLI write completed after ${newer} cannot commit or restore an obsolete model`, async () => {
    const flow = await manualFeedbackFlow();
    await flow.select('opus');
    await flow.render();
    let release = () => {};
    let started = () => {};
    const writing = new Promise<void>(resolve => {started = resolve;});
    const gate = new Promise<void>(resolve => {release = resolve;});
    flow.state.offWrite = async () => {started(); await gate;};
    const submission = flow.state.reason('The routed model was too slow');
    await writing;
    if (newer === 'prompt') await flow.prompt();
    else if (newer === 'session end') await flow.end();
    else await flow.select('ug-auto-fable');
    const current = flow.state.model;
    const commands = flow.state.nativeModels.length;
    release();
    await submission;
    expect(flow.state.model).toBe(current);
    expect(flow.state.nativeModels).toHaveLength(commands);
    expect(flow.state.logs).not.toContain('Smart Router is off');
    if (newer !== 'session end') {
      expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
      expect(flow.state.controls.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    }
  });
}
