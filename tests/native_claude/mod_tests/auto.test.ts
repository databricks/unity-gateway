import { expect, mock, test } from 'claude-code/testing';
import { register } from '../hooks/register.ts';

import { config, options, start } from './auto-fixtures.ts';

const warmSwitch = {
  from_model:'opus', to_model:'ug-auto-opus', requested_model:'ug-auto-opus', source:'picker' as const,
  context_tokens:50_000, prompt_cache_warm:true, cache_ttl:'5m' as const,
  estimated_cache_write_usd:1, pricing:'catalog' as const,
};

test('only a configured Auto alias for the same deployment and context bypasses the cache warning', {options}, async ($, on) => {
  const {state} = host(on, 'opus');
  const settings = {modelOverrides:{'ug-auto-opus':'opus','other-deployment':'opus'}};
  state.settings = settings;
  on('classic.PreModelSwitch', () => ({}));
  expect((await $.classic.PreModelSwitch(warmSwitch)).permissionDecision).toBe('allow');
  expect((await $.classic.PreModelSwitch({...warmSwitch,from_model:'other-deployment'})).permissionDecision).toBe('allow');
  for (const change of [
    {from_model:'fable'}, {from_model:'opus[1m]'}, {to_model:'opus',requested_model:'opus'},
    {to_model:'ug-auto-unknown',requested_model:'ug-auto-unknown'}, {requested_model:'Auto'},
  ]) expect((await $.classic.PreModelSwitch({...warmSwitch,...change})).permissionDecision).toBeUndefined();
});

for (const decision of [{permissionDecision:'deny' as const,permissionDecisionReason:'Policy'},
  {permissionDecision:'ask' as const,permissionDecisionReason:'Confirm'}, {block:'blocked'}, {preventContinuation:true as const}]) {
  test(`Auto equivalence preserves the downstream ${JSON.stringify(decision)} decision`, {options}, async ($, on) => {
    const {state} = host(on, 'opus');
    state.settings = {modelOverrides:{'ug-auto-opus':'opus'}};
    on('classic.PreModelSwitch', () => decision);
    expect(await $.classic.PreModelSwitch(warmSwitch)).toEqual(decision);
  });
}

for (const modelOverrides of [undefined, null, [], 'invalid', {'ug-auto-opus':null}, {'ug-auto-opus':'different'}, {'ug-auto-opus':'opus',opus:'other'}]) {
  test(`Auto cache equivalence fails closed for ${JSON.stringify(modelOverrides)}`, {options}, async ($, on) => {
    const {state} = host(on, 'opus');
    state.settings = {modelOverrides};
    on('classic.PreModelSwitch', () => ({}));
    expect((await $.classic.PreModelSwitch(warmSwitch)).permissionDecision).toBeUndefined();
  });
}

const millionConfig = {...config, models:[
  {model:'system.ai.claude-opus-5-5',label:'Opus 5.5',autoModel:'ug-auto-8c4cd93ee5ac-claude-opus-5-5'},
  {model:'system.ai.claude-sonnet-5-5',label:'Sonnet 5.5',autoModel:'ug-auto-1d012b8c9d17-claude-sonnet-5-5'},
]};

for (const row of millionConfig.models) {
  test(`the native 1M ${row.label} picker variant matches its verified bare Auto target`,
    {options:{...options,autoPicker:JSON.stringify(millionConfig)}}, async ($, on) => {
      const {state,env} = host(on, row.model + '[1m]');
      state.settings = {modelOverrides:Object.fromEntries(millionConfig.models.map(row => [row.autoModel,row.model]))};
      on('classic.PreModelSwitch', () => ({}));
      on('command.run', {command:'model'}, () => ({ref:1}));
      await $.command.run({command:'model',args:''});
      expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe(row.autoModel);
      const event = {...warmSwitch,from_model:row.model + '[1m]',to_model:row.autoModel,requested_model:row.autoModel};
      expect((await $.classic.PreModelSwitch(event)).permissionDecision).toBe('allow');
      state.window = 200_000;
      expect((await $.classic.PreModelSwitch(event)).permissionDecision).toBeUndefined();
      state.window = 1_000_000;
      expect((await $.classic.PreModelSwitch({...event,from_model:row.model + '[200k]'})).permissionDecision).toBeUndefined();
      const other = millionConfig.models.find(other => other !== row)!;
      expect((await $.classic.PreModelSwitch({...event,to_model:other.autoModel,requested_model:other.autoModel})).permissionDecision).toBeUndefined();
    });
}

function host(on: any, selected = 'ug-auto-opus') {
  const settings: {modelOverrides?: unknown; maxEffortLevel?: string} = {};
  const state = {selected, effortStatus:'Effort level: auto (currently medium)', window:1_000_000, rejectModel: false, processExitCode: 0, agents: [] as any[], toasts: [] as string[], modelResult: {text: 'Set model to `model`'} as any, settings, saved: undefined as unknown, questions: [] as any[], closed: [] as any[], appendClosed: [] as number[], appended: [] as string[], invalidations: [] as string[], calls: [] as any[], forwarded: [] as any[], tails: [] as any[]};
  const env: Record<string, string | undefined> = {
    ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: '0', UCODE_SMART_ROUTER_PYTHON: '/ug/python',
    UCODE_SESSION_ENV_FILE: '/session/env.json',
  };
  const clock = mock.clock(on);
  on('env.get', ($: any, e: any) => ({value: env[e.name]}));
  on('env.set', ($: any, e: any) => {env[e.name] = e.value; return {value: undefined};});
  on('fs.read', () => ({value: '{}'}));
  on('command.run', {command: 'model'}, async ($: any, e: any, next: any) => {
    if (!e.args) return next(e);
    if (!state.rejectModel) state.selected = e.args;
    return state.modelResult;
  });
  on('command.run', {command:'effort'}, () => ({text:state.effortStatus}));
  on('settings.read', () => ({value:state.settings}));
  on('session.usage', () => ({value:{context:{window:state.window}}}));
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
function commandTranscript({state, env}: ReturnType<typeof host>, pluginOptions = options) {
  let append: any;
  let model: any;
  register(((event: string, ...args: any[]) => {
    if (event === 'session.append' && args[0]?.door === 'command' && !append) append = args.at(-1);
    if (event === 'command.run' && args[0]?.command === 'model' && !model) model = args.at(-1);
  }) as any, pluginOptions);
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
  const {state, clock} = host(on, 'ug-auto-opus');
  await $.session.start(start);
  await step($);
  expect(state.forwarded[0].model).toBe('opus');
  expect(state.selected).toBe('ug-auto-opus');
  expect(await hint($, state)).toBe('native · Auto (Opus 5.5) · smart router ON');
  await step($, 'child');
  expect(state.forwarded[1].model).toBe('native');
});

test('Auto bands show successful model changes and hide after the next completed prompt', {options}, async ($, on) => {
  const {state, clock} = host(on, 'ug-auto-opus');
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
  await clock.advance(0);
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
  const {state, env, clock} = host(on, 'opus');
  await $.session.start(start);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  await $.command.run({command: 'auto'});
  expect(state.questions[0].id).toBe('auto-main');
  const pane = await $.ui.mount({plugin: 'ug-smart-router', component: 'Pane', requestId: 'auto-main', surface: 'terminal', props: {} as any});
  await pane.press({key:'auto-main-model-3'});
  await clock.advance(0);
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

test('/model auto enters Auto on the current permitted target before opening its controls', {options}, async ($, on) => {
  const {state, env, clock} = host(on, 'opus');
  state.saved = 'astra';
  await $.session.start(start);
  await $.command.run({command:'model', args:'auto'});
  expect(state.selected).toBe('ug-auto-opus');
  expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe('ug-auto-opus');
  expect(state.saved).toBe('astra');
  expect(state.questions.length).toBe(1);
  expect(state.questions[0].id).toBe('auto-main');
  await step($);
  expect(state.forwarded[0].model).toBe('opus');
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

test('Auto help exposes /model auto and hides the internal /auto command', {options}, async ($, on) => {
  host(on, 'ug-auto-opus');
  on('command.describe', ($: any, e: any) => ({description:e.description, isHidden:e.isHidden}));
  const description = await $.command.describe({command:'auto', description:'Customize main agent model',
    isHidden:false, immediate:true, provider:{plugin:'ug-smart-router', tier:'user'}});
  expect(description.isHidden).toBe(true);
  const reply = await $.command.run({command:'smart-router', args:'on'});
  expect(reply.text).toContain('Use /model auto');
  expect(reply.text).not.toContain('Use /auto');
});

test('a removed saved model preserves a valid default', {options}, async ($, on) => {
  const {state, clock} = host(on, 'ug-auto-opus');
  state.saved = 'removed';
  await $.command.run({command: 'auto'});
  expect(state.saved).toBe('removed');
  await step($);
  expect(state.forwarded[0].model).toBe('opus');
});

test('reselecting Auto opens the next step and manual selection disables routing', {options}, async ($, on) => {
  const context = host(on, 'ug-auto-opus');
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

test('Auto confirmations omit the native default-save claim and preserve effort and warnings', {options}, async ($, on) => {
  const context = host(on, 'ug-auto-opus');
  const confirm = commandTranscript(context);
  const warnings = '\nA native warning\nNote: ANTHROPIC_MODEL is set and may override this model choice.';
  for (const [details, remaining] of [
    [' and saved as your default for new sessions with `high` effort' + warnings, '\nWith `high` effort' + warnings],
    [' and saved as your default for new sessions' + warnings, warnings],
    [' and saved as your default for new sessions', ''],
    [' saved as your default for new sessions', ''],
    [' with `high` effort' + warnings, '\nWith `high` effort' + warnings],
  ]) {
    for (const action of ['Set model to', 'Kept model as']) {
      context.state.selected = 'ug-auto-opus';
      await confirm(`${action} \`Opus 5.5\`` + details);
      expect(context.state.appended.at(-1)).toBe(`<local-command-stdout>${action === 'Set model to' ? 'Set to' : 'Kept'} Auto mode with main model \`opus\`. Subagents are smart routed.${remaining}</local-command-stdout>`);
      expect(context.state.selected).toBe('ug-auto-opus');
      context.state.selected = 'opus';
      await confirm(`${action} \`Opus 5.5\`` + details);
      expect(context.state.appended.at(-1)).toBe(`<local-command-stdout>${action} \`Opus 5.5\`` + details + '</local-command-stdout>');
    }
  }
  context.state.selected = 'ug-auto-opus';
  await confirm('Kept model as `Opus 5.5`');
  expect(context.state.appended.at(-1)).toBe('<local-command-stdout>Kept Auto mode with main model `opus`. Subagents are smart routed.</local-command-stdout>');
});

for (const row of millionConfig.models) {
  test(`Auto confirmations name the active ${row.label} deployment`,
    {options:{...options,autoPicker:JSON.stringify(millionConfig)}}, async ($, on) => {
      const context = host(on, row.autoModel);
      const confirm = commandTranscript(context, {...options,autoPicker:JSON.stringify(millionConfig)});
      for (const action of ['Set model to', 'Kept model as']) {
        await confirm(`${action} \`${row.autoModel}\``);
        expect(context.state.appended.at(-1)).toBe(
          `<local-command-stdout>${action === 'Set model to' ? 'Set to' : 'Kept'} Auto mode with main model \`${row.model.replace(/^system\.ai\./, '')}\`. Subagents are smart routed.</local-command-stdout>`);
      }
    });
}

test('only UG internal model-command echoes relabel configured Auto aliases', {options}, async ($, on) => {
  const context = host(on, 'ug-auto-opus');
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
    const context = host(on, 'ug-auto-opus');
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
  const context = host(on, 'ug-auto-opus');
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


for (const confirmation of ['Set model to `model`', 'Set to Auto mode with main model `astra`. Subagents are smart routed.']) {
  test(`/auto saves after the native confirmation: ${confirmation}`, {options}, async ($, on) => {
    const {state, env, clock} = host(on, 'ug-auto-opus');
    state.modelResult = {text:confirmation};
    await $.session.start(start);
    await $.command.run({command: 'auto'});
    const pane = await $.ui.mount({plugin: 'ug-smart-router', component: 'Pane', requestId: 'auto-main', surface: 'terminal', props: {} as any});
    await pane.press({key:'auto-main-model-3'});
    await clock.advance(0);
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
}


test('manual selection and cancellation close Auto before the native picker confirmation renders', {options}, async ($, on) => {
  const context = host(on, 'ug-auto-opus');
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
  const {state, env, clock} = host(on, 'opus');
  await $.session.start(start);
  expect(env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBeUndefined();
  expect(state.questions).toHaveLength(0);
  expect(await hint($, state)).toBe('native · smart router ON');
});

test('a rejected main-model switch preserves the current selection and preference', {options}, async ($, on) => {
  const {state, env, clock} = host(on, 'ug-auto-opus');
  state.rejectModel = true;
  state.modelResult = {text:'Model not permitted'};
  await $.session.start(start);
  await $.command.run({command:'auto'});
  const pane = await $.ui.mount({plugin:'ug-smart-router', component:'Pane', requestId:'auto-main', surface:'terminal', props:{} as any});
  await pane.press({key:'auto-main-model-3'});
  await clock.advance(0);
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
  const {state, env, clock} = host(on, 'ug-auto-opus');
  env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY = '0';
  state.processExitCode = 1;
  await step($);
  await step($);
  expect(state.forwarded.length).toBe(2);
  expect(state.toasts.length).toBe(1);
  expect(state.toasts[0]).toContain('Could not update Smart Routing');
});

test('rejected main model reopens the pane and shows a retry message', {options}, async ($, on) => {
  const {state, clock} = host(on, 'ug-auto-opus');
  state.rejectModel = true;
  await $.command.run({command:'auto'});
  const pane = await $.ui.mount({plugin:'ug-smart-router', component:'Pane', requestId:'auto-main', surface:'terminal', props:{} as any});
  await pane.press({key:'auto-main-model-3'});
  await clock.advance(0);
  expect(state.selected).toBe('ug-auto-opus');
  expect(state.saved).toBeUndefined();
  expect(state.closed.length).toBe(1);
  expect(state.questions.at(-1).id).toBe('auto-main');
  expect(state.toasts.at(-1)).toContain('Could not save Auto');
  await pane.unmount();
});

for (const direction of ['auto-effort-less','auto-effort-more']) {
  test(`native max effort capped at high adjusts without wrapping via ${direction}`, {options}, async ($, on) => {
    const {state, clock} = host(on);
    state.settings.maxEffortLevel = 'high';
    state.effortStatus = 'Current effort level: max';
    await $.command.run({command:'auto'});
    await clock.advance(0);
    const pane = await $.ui.mount({plugin:'ug-smart-router', component:'Pane', requestId:'auto-main', surface:'terminal', props:{} as any});
    expect(JSON.stringify(await pane.drawn())).toContain('High effort');
    await pane.press({key:direction});
    expect(JSON.stringify(await pane.drawn())).toContain(direction === 'auto-effort-less' ? 'Medium effort' : 'High effort');
    await pane.press({key:'auto-main-model-0'});
    await clock.advance(0);
    expect(state.saved).toBe('opus');
    await pane.unmount();
  });
}

test('manual models still show the running agent count', {options}, async ($, on) => {
  const {state} = host(on, 'opus');
  await $.session.start(start);
  state.agents = [{id:'a',status:'running'}];
  expect(await hint($,state)).toBe('native · smart router OFF · 1 subagent running');
});
