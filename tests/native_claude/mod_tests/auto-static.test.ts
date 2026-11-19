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

// Component-only engine: native terminal behavior is checked separately.
function nativeSession(on: On, initial: string, saved?: string) {
  let selected = initial;
  const store: Record<string, unknown> = saved ? {[AUTO.storeKey]:saved} : {};
  const env: Record<string, string | undefined> = {
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY:'1', ENABLE_SMART_ROUTING_V2:'0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR:'0', ENABLE_SMART_ROUTER_AUTO_MODE:'1',
    UCODE_SMART_ROUTER_PYTHON:'/ug/python', UCODE_SESSION_ENV_FILE:'/session/env.json',
  };
  const commands: {command:string; args:string}[] = [];
  const notices: string[] = [];
  const toasts: string[] = [];
  const panes: string[] = [];
  const runs: readonly string[][] = [];
  const failure: {open?:boolean; select?:string} = {};
  on('env.get', ($, e) => ({value:env[e.name]}));
  on('env.set', ($, e) => {env[e.name] = e.value; return {value:undefined};});
  on('session.model', () => ({value:selected}));
  on('session.usage', () => ({value:{context:{window:200000, tokens:1000}}}));
  on('settings.read', () => ({value:{modelOverrides:{[DEFAULT_ALIAS]:MAIN, [OTHER_ALIAS]:OTHER}}}));
  on('store.get', ($, e) => ({value:store[e.key]}));
  on('store.set', ($, e) => {store[e.key] = e.value; return {value:undefined};});
  on('fs.read', () => ({value:'{}'}));
  on('process.run', ($, e) => {runs.push(e.argv); return {value:{exitCode:0, stdout:'', stderr:''}};});
  on('command.register', () => ({value:undefined}));
  on('command.run', ($, e) => {
    commands.push({command:e.command, args:e.args});
    if (e.command === 'model' && !e.args && failure.open) throw new Error('picker unavailable');
    if (e.command === 'model' && e.args) {
      if (failure.select === e.args) return {text:'Model switch cancelled'};
      selected = e.args;
      return {text:'Set model to ' + selected};
    }
    return {ref:1};
  });
  on('ui.open', ($, e) => {panes.push(e.id); return {value:{isPlaced:true}};});
  on('ui.close', () => ({value:undefined}));
  on('ui.toast', ($, e) => {toasts.push(e.text); return {value:undefined};});
  on('ui.log', ($, e) => {notices.push(e.text); return {value:undefined};});
  on('ui.invalidate', () => ({value:undefined}));
  on('session.start', () => ({cwd:'/work'}));
  on('session.end', () => ({sessionId:'session'}));
  const clock = mock.clock(on);
  return {env, store, commands, notices, toasts, panes, runs, failure, clock,
    selected:() => selected, select:(model:string) => {selected = model;}};
}

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

test('Auto initializes routing and its single custom identity', {options:OPTIONS}, async ($, on) => {
  const session = nativeSession(on, DEFAULT_ALIAS);
  await $.session.start(START);
  expect(session.env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe(DEFAULT_ALIAS);
  expect(session.env.ANTHROPIC_CUSTOM_MODEL_OPTION_NAME).toBe('Auto');
  expect(session.env.ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION).toBe(DESCRIPTION);
  expect(session.runs.some(argv => argv.includes('--enable-smart-routing'))).toBe(true);
});

for (const args of ['', MAIN]) {
  test(`ordinary /model ${args} does not open a second picker`, {options:OPTIONS}, async ($, on) => {
    const session = nativeSession(on, DEFAULT_ALIAS);
    await $.session.start(START);
    await $.command.run({command:'model', args});
    await session.clock.advance(0);
    expect(session.commands).toEqual([{command:'model', args}]);
    expect(session.notices).toEqual([]);
  });
}

test('/model auto opens the native main picker directly', {options:OPTIONS}, async ($, on) => {
  const session = nativeSession(on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({command:'model', args:'auto'});
  await session.clock.advance(0);
  expect(session.commands.at(-1)).toEqual({command:'model', args:''});
  expect(session.notices[0]).toContain('Choose your main model.');
});

test('an interrupted scheduled chooser cannot open later', {options:OPTIONS}, async ($, on) => {
  const session = nativeSession(on, DEFAULT_ALIAS);
  await $.session.start(START);
  await $.command.run({command:'auto', args:''});
  await $.command.run({command:'model', args:OTHER});
  await session.clock.advance(0);
  expect(session.commands).toEqual([{command:'model', args:OTHER}]);
  expect(session.store[AUTO.storeKey]).toBe(undefined);
});

for (const decision of ['deny', 'ask'] as const) {
  test(`native preparation preserves another hook's ${decision}`, {options:OPTIONS}, async ($, on) => {
    nativeSession(on, DEFAULT_ALIAS);
    on('classic.PreModelSwitch', () => ({permissionDecision:decision}));
    await $.session.start(START);
    expect((await $.classic.PreModelSwitch({from_model:DEFAULT_ALIAS, to_model:MAIN, requested_model:MAIN,
      source:'command', context_tokens:1000, prompt_cache_warm:true, cache_ttl:'5m', estimated_cache_write_usd:0.01, pricing:'catalog'})).permissionDecision).toBe(decision);
  });
}

for (const decision of ['allow', 'deny', 'ask'] as const) {
  test(`managed model-switch forwarding preserves ${decision} without skipping UG`, {
    options: OPTIONS,
    plugins: [
      {
        name: 'model-policy-first', tier: 'prepend',
        register(on) {
          on('classic.PreModelSwitch', async ($, e, next) => {
            const result = await next.to(e, 'append');
            $.ui.log(JSON.stringify(next.trace.map(link => ({plugin:link.plugin, outcome:link.outcome}))));
            return result;
          });
        },
      },
      {
        name: 'model-policy-final', tier: 'append',
        register(on) {
          on('classic.PreModelSwitch', ($, e) => ({
            permissionDecision: e.requested_model === 'deny' ? 'deny' : e.requested_model === 'ask' ? 'ask' : 'allow',
          }));
        },
      },
    ],
  }, async ($, on) => {
    const traces: {plugin:string; outcome:string}[][] = [];
    on('ui.log', ($, e) => {traces.push(JSON.parse(e.text)); return {value:undefined};});
    const result = await $.classic.PreModelSwitch({from_model:DEFAULT_ALIAS, to_model:MAIN, requested_model:decision,
      source:'command', context_tokens:1000, prompt_cache_warm:true, cache_ttl:'5m', estimated_cache_write_usd:0.01, pricing:'catalog'});
    expect(result.permissionDecision).toBe(decision);
    expect(traces.length).toBe(1);
    expect(traces[0].some(link => link.plugin === 'ug-smart-router' && link.outcome === 'skipped')).toBe(false);
  });
}

test('saved static main models and inherited aliases keep their real targets', {options:OPTIONS}, async ($, on) => {
  nativeSession(on, DEFAULT_ALIAS, OTHER);
  const sent = captureSteps(on);
  await $.session.start(START);
  await step($, DEFAULT_ALIAS);
  await step($, DEFAULT_ALIAS, 'child');
  await step($, OTHER_ALIAS, 'child');
  await step($, 'system.ai.claude-opus-5', 'child');
  expect(sent).toEqual([OTHER, OTHER, OTHER, 'system.ai.claude-opus-5']);
});

for (const count of [2, 10, 23]) {
  test(`catalog of ${count} models delegates the list to Claude without opening a pane`, {
    options:{...OPTIONS, autoPicker:JSON.stringify({...AUTO, models:[...AUTO.models,
      ...Array.from({length:count - 2}, (_, i) => ({model:'test-' + i, label:'Model ' + i, autoModel:'ug-auto-' + i}))]})},
  }, async ($, on) => {
    const session = nativeSession(on, DEFAULT_ALIAS);
    await $.session.start(START);
    await $.command.run({command:'auto', args:''});
    await session.clock.advance(0);
    expect(session.commands.at(-1)).toEqual({command:'model', args:''});
    expect(session.selected()).toBe(MAIN);
    expect(session.panes).toEqual([]);
    expect(session.store[AUTO.storeKey]).toBe(undefined);
    expect(session.commands.some(command => command.command === 'effort')).toBe(false);
  });
}

for (const failure of ['prepare', 'open']) {
  test(`a native ${failure} failure keeps the previous Auto preference`, {options:OPTIONS}, async ($, on) => {
    const session = nativeSession(on, DEFAULT_ALIAS, MAIN);
    if (failure === 'prepare') session.failure.select = MAIN;
    else session.failure.open = true;
    await $.session.start(START);
    await $.command.run({command:'auto', args:''});
    await session.clock.advance(0);
    expect(session.selected()).toBe(DEFAULT_ALIAS);
    expect(session.store[AUTO.storeKey]).toBe(MAIN);
    expect(session.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    expect(session.toasts.at(-1)).toContain("Could not open Claude's model picker");
    expect(session.notices.some(text => text.startsWith('Auto mode:'))).toBe(false);
  });
}
