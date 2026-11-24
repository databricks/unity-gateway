import type { On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';
import {
  SMART_ROUTER_OFF_REPLY, UNSUPPORTED_MODEL_LIST_WARNING, UNSUPPORTED_ROUTER_STATE,
  VALIDATION_FAILED_WARNING,
} from '../hooks/constants.ts';

const ROUTED = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '0',
  unset: [],
};
const VALIDATED = { ...ROUTED, startupValidation: '1' };
const OFF = '{"ENABLE_SMART_ROUTING_V2":"0","ENABLE_SMART_ROUTING_SUBAGENT_ONLY":"0","ENABLE_SMART_ROUTER_ORCHESTRATOR":"0"}';
const WARNING = "This session's model list isn't supported by smart router";
const FAILED = {
  routing_disabled: true, validation_failed: true, validation_failure_reason: "Smart Router didn't respond in time",
  router_name: 'stable', eligible_route_options: [], ineligible_route_options: [], available_route_options: [], validation_ms: 4000,
};
const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;
const abovePrompt = {
  plugin: 'ug-smart-router', component: 'AbovePrompt', surface: 'terminal', props: {},
} as const;

// The engine beneath the mod: a routed environment, the session-controls file Python owns,
// and a validation result that stays pending until the test publishes it.
function engine(on: On) {
  const clock = mock.clock(on);
  const env: Record<string, string | undefined> = {
    ...ROUTED,
    CLAUDE_CODE_EXTRA_BODY: '{}',
    UCODE_SESSION_ENV_FILE: '/session/env.json',
  };
  const files: Record<string, string | undefined> = { '/session/env.json': '{}' };
  const state = { reads: 0, opened: 0, notices: [] as unknown[], tails: [] as unknown[] };
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('session.start', () => ({ cwd: '/resolved-work' }));
  on('session.id', () => ({ value: 'session-one' }));
  on('fs.read', ($, e) => {
    if (e.path === '/session/env.validation.json') state.reads += 1;
    const value = files[e.path];
    if (value === undefined) throw new Error('pending');
    return { value };
  });
  on('ui.open', () => { state.opened++; return { value: { isPlaced: true } }; });
  on('ui.toast', () => { state.opened++; return { value: undefined }; });
  on('session.append', ($, e, next) => {
    state.notices.push(e.message);
    return next(e);
  });
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['host content'] }));
  on('ui.render', { component: 'PromptHint' }, ($, e) => {
    state.tails.push(e.props.tail);
    return $.ui.resolve(e).Text({ children: ['hint'] });
  });
  on('agent.list', () => ({ value: [] }));
  const hint = async ($: any): Promise<unknown> => {
    state.tails.length = 0;
    await $.ui.mount({ ...abovePrompt, component: 'PromptHint', props: { tail: '' } });
    return state.tails.at(-1);
  };
  return { clock, env, files, state, hint };
}

const cases = [
  { name: 'supported', result: { routing_disabled: false }, routed: true, warning: undefined },
  { name: 'unsupported', result: { routing_disabled: true }, routed: false, warning: UNSUPPORTED_MODEL_LIST_WARNING },
  { name: 'failed', result: FAILED, routed: false, warning: VALIDATION_FAILED_WARNING },
];

for (const { name, result, routed, warning } of cases) {
  test(`startup validation ${name} holds the band until the result`, { options: VALIDATED }, async ($, on) => {
    const { clock, env, files, state, hint } = engine(on);

    // Claude draws before session.start; the band is held from the first paint.
    const ui = await $.ui.mount(abovePrompt);
    expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
    expect(await $.session.start(start)).toEqual({ cwd: '/resolved-work' });
    await clock.advance(1000);
    const pending = JSON.stringify(await ui.drawn());
    expect(pending).toContain('host content');
    expect(pending).not.toContain('unity gateway smart router');
    expect(await hint($)).toBe('');

    // Python writes the session controls before it publishes a disabling result.
    if (!routed) files['/session/env.json'] = OFF;
    files['/session/env.validation.json'] = JSON.stringify(result);
    await clock.advance(100);
    const shown = JSON.stringify(await ui.drawn());
    expect(shown).toContain('host content');
    expect(shown).toContain('unity gateway smart router');
    for (const text of [UNSUPPORTED_MODEL_LIST_WARNING, VALIDATION_FAILED_WARNING]) {
      expect(shown.includes(text)).toBe(text === warning);
    }
    expect(await hint($)).toBe(`smart router ${routed ? 'ON' : UNSUPPORTED_ROUTER_STATE}`);
    expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe(routed ? '1' : '0');
    // No recipe writer runs here: only the session mod is loaded, and ug's managed
    // policy-tier mod owns the extra body.
    expect(env.CLAUDE_CODE_EXTRA_BODY).toBe('{}');
    expect(state.opened).toBe(0);
    expect(state.notices).toEqual([]);

    const reads = state.reads;
    await clock.advance(2000);
    expect(state.reads).toBe(reads);
    await ui.unmount();
  });
}

const omniLogCases = [
  { name: 'an unsupported result in Omnigent logs the off reply and the warning', launchMode: 'omni', result: { routing_disabled: true }, warning: UNSUPPORTED_MODEL_LIST_WARNING },
  { name: 'a failed check in Omnigent logs the off reply and the validation warning', launchMode: 'omni', result: FAILED, warning: VALIDATION_FAILED_WARNING },
  { name: 'an unsupported result outside Omnigent logs nothing', launchMode: undefined, result: { routing_disabled: true }, warning: undefined },
  { name: 'a supported result in Omnigent logs nothing', launchMode: 'omni', result: { routing_disabled: false }, warning: undefined },
];

for (const { name, launchMode, result, warning } of omniLogCases) {
  test(name, { options: VALIDATED }, async ($, on) => {
    const { clock, env, files } = engine(on);
    const logs: string[] = [];
    on('ui.log', ($, e) => { logs.push(e.text); return { value: undefined }; });
    env.ISAAC_LAUNCH_MODE = launchMode;
    const ui = await $.ui.mount(abovePrompt);
    await $.session.start(start);
    if (result.routing_disabled) files['/session/env.json'] = OFF;
    files['/session/env.validation.json'] = JSON.stringify(result);
    await clock.advance(100);
    expect(logs).toEqual(launchMode && warning ? [SMART_ROUTER_OFF_REPLY, warning] : []);
    await ui.unmount();
  });
}

test('startup validation shows the band ON after 5 seconds without a result and stops polling', { options: VALIDATED }, async ($, on) => {
  const { clock, state, hint } = engine(on);
  const ui = await $.ui.mount(abovePrompt);
  await $.session.start(start);
  await clock.advance(4900);
  expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
  await clock.advance(100);
  const shown = JSON.stringify(await ui.drawn());
  expect(shown).toContain('unity gateway smart router');
  expect(shown).toContain(' ON ');
  expect(await hint($)).toBe('smart router ON');
  const reads = state.reads;
  await clock.advance(2000);
  expect(state.reads).toBe(reads);
  expect(state.notices).toEqual([]);
  await ui.unmount();
});

test('a result already written by session start shows the band without polling', { options: VALIDATED }, async ($, on) => {
  const { clock, files, state, hint } = engine(on);
  files['/session/env.validation.json'] = '{"routing_disabled":false}';
  const ui = await $.ui.mount(abovePrompt);
  expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
  await $.session.start(start);
  expect(JSON.stringify(await ui.drawn())).toContain('unity gateway smart router');
  expect(await hint($)).toBe('smart router ON');
  await clock.advance(1000);
  expect(state.reads).toBe(1);
  await ui.unmount();
});

test('a session without controls shows the band once session start runs', { options: VALIDATED }, async ($, on) => {
  const { env, state } = engine(on);
  env.UCODE_SESSION_ENV_FILE = undefined;
  const ui = await $.ui.mount(abovePrompt);
  expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
  await $.session.start(start);
  expect(JSON.stringify(await ui.drawn())).toContain('unity gateway smart router');
  expect(state.reads).toBe(0);
  await ui.unmount();
});

const turnCases = [
  { name: 'a turn that ends while the check is pending does not hide the band', endsBeforeResult: true, routed: true },
  { name: 'a band that appears mid-turn hides when that turn ends', endsBeforeResult: false, routed: true },
  { name: 'the unsupported warning hides when the next turn ends', endsBeforeResult: true, routed: false },
  { name: 'the validation-failed warning hides when the next turn ends', endsBeforeResult: true, routed: false, failed: true },
];

for (const { name, endsBeforeResult, routed, failed = false } of turnCases) {
  test(name, { options: VALIDATED }, async ($, on) => {
    const { clock, files, hint } = engine(on);
    on('prompt.submit', ($, e) => ({ text: e.text }));
    on('turn.complete', () => ({ text: '' }));
    const ui = await $.ui.mount(abovePrompt);
    await $.session.start(start);
    await $.prompt.submit({ text: 'early prompt' });
    if (endsBeforeResult) await $.turn.complete({ answer: 'done' });
    if (!routed) files['/session/env.json'] = OFF;
    files['/session/env.validation.json'] = JSON.stringify(failed ? FAILED : { routing_disabled: !routed });
    await clock.advance(100);
    expect(JSON.stringify(await ui.drawn())).toContain(routed ? 'unity gateway smart router' : failed ? VALIDATION_FAILED_WARNING : WARNING);
    if (!routed) {
      await $.prompt.submit({ text: 'next prompt' });
      await $.turn.complete({ answer: 'done' });
      expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
      expect(await hint($)).toBe(`smart router ${UNSUPPORTED_ROUTER_STATE}`);
    }
    if (!endsBeforeResult) {
      await $.turn.complete({ answer: 'done' });
      expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
    }
    await ui.unmount();
  });
}

test('without route validation the band shows from the first paint', { options: ROUTED }, async ($, on) => {
  const { state } = engine(on);
  const ui = await $.ui.mount(abovePrompt);
  expect(JSON.stringify(await ui.drawn())).toContain('unity gateway smart router');
  await $.session.start(start);
  expect(state.reads).toBe(0);
  await ui.unmount();
});

for (const disabled of [true, false]) {
  test(`${disabled ? 'an unsupported' : 'a supported'} result ${disabled ? 'records' : 'records no'} Smart Router logs row`, {
    options: { ...VALIDATED, logsCommand: ['/bin/ug', 'smart-router-logs'] },
  }, async ($, on) => {
    const { clock, files } = engine(on);
    const runs: string[][] = [];
    on('process.run', ($, e) => { runs.push(e.argv); return { value: { exitCode: 0, stderr: '', stdout: '{"v":1}' } }; });
    const ui = await $.ui.mount(abovePrompt);
    await $.session.start(start);
    if (disabled) files['/session/env.json'] = OFF;
    files['/session/env.validation.json'] = JSON.stringify({ routing_disabled: disabled });
    await clock.advance(100);
    expect(runs).toEqual(disabled ? [[
      '/bin/ug', 'smart-router-logs', '--session', 'session-one',
      '--record-route-validation', '/session/env.validation.json',
    ]] : []);
    await ui.unmount();
  });
}
