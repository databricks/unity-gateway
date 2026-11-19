import type { On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';
import { DISABLED_RECIPE, RECIPE_FIELD } from '../hooks/constants.ts';

const ROUTED = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '0',
  unset: [],
  recipe: 'task_v3',
};
const OFF = '{"ENABLE_SMART_ROUTING_V2":"0","ENABLE_SMART_ROUTING_SUBAGENT_ONLY":"0","ENABLE_SMART_ROUTER_ORCHESTRATOR":"0"}';
const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;
const abovePrompt = {
  plugin: 'ug-smart-router', component: 'AbovePrompt', surface: 'terminal', props: {},
} as const;
const UNSUPPORTED = 'Smart Router does not support this model menu. Available options: claude-sonnet (claude_code), glm-5 (claude_code). Smart routing is off for this session.';
const UNAVAILABLE = 'Smart Router compatibility check unavailable. Routing remains enabled.';
const firstSentence = (message: string): string => message.split(/(?<=\.)\s/, 1)[0];

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
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: '' }));
  on('agent.list', () => ({ value: [] }));
  const hint = async ($: any): Promise<unknown> => {
    state.tails.length = 0;
    await $.ui.mount({ ...abovePrompt, component: 'PromptHint', props: { tail: '' } });
    return state.tails.at(-1);
  };
  return { clock, env, files, state, hint };
}

const cases = [
  { name: 'supported', result: { message: null }, box: '', routed: true },
  { name: 'unsupported', result: { message: UNSUPPORTED, routing_disabled: true }, box: '', routed: false },
  { name: 'unavailable', result: { message: UNAVAILABLE }, box: firstSentence(UNAVAILABLE), routed: true },
];

for (const { name, result, box, routed } of cases) {
  for (const submittedBeforeResult of [false, true]) {
    test(`startup validation ${name} holds the band until the result, with earlier prompt ${submittedBeforeResult}`, { options: ROUTED }, async ($, on) => {
      const { clock, env, files, state, hint } = engine(on);

      // Claude draws before session.start; the band is held from the first paint.
      const ui = await $.ui.mount(abovePrompt);
      expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
      expect(await $.session.start(start)).toEqual({ cwd: '/resolved-work' });
      const pending = JSON.stringify(await ui.drawn());
      expect(pending).toContain('host content');
      expect(pending).not.toContain('unity gateway smart router');
      expect(await hint($)).toBe('');
      await clock.advance(1000);
      expect(state.reads).toBe(11);
      expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
      if (submittedBeforeResult) {
        expect((await $.prompt.submit({ text: 'before validation' })).text).toBe('before validation');
      }

      // Python writes the session controls before it publishes a disabling result.
      if (!routed) files['/session/env.json'] = OFF;
      files['/session/env.validation.json'] = JSON.stringify(result);
      await clock.advance(100);
      const shown = JSON.stringify(await ui.drawn());
      expect(shown).toContain('host content');
      expect(shown.includes(' ON ')).toBe(routed);
      expect(shown.includes('unity gateway smart router')).toBe(routed);
      expect(await hint($)).toBe(routed ? 'smart router ON' : 'smart router OFF');
      expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe(routed ? '1' : '0');
      expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY as string)[RECIPE_FIELD])
        .toBe(routed ? 'task_v3' : DISABLED_RECIPE);
      expect(shown.includes('Ctrl+O for Smart Router validation details')).toBe(Boolean(box));
      if (box) {
        expect(shown).toContain(box);
        expect(shown).toContain('"borderStyle":"round"');
        expect(shown).not.toContain('Routing remains enabled.');
      }
      expect(state.notices).toEqual(result.message ? [{
        type: 'system',
        content: [{ type: 'text', text: 'Smart Router validation\n' + result.message }],
      }] : []);

      if (submittedBeforeResult) {
        await $.turn.complete({ answer: 'earlier turn done' });
        const afterEarlierTurn = JSON.stringify(await ui.drawn());
        expect(afterEarlierTurn).toContain('host content');
        expect(afterEarlierTurn).not.toContain('unity gateway smart router');
        expect(afterEarlierTurn.includes('Ctrl+O for Smart Router validation details')).toBe(Boolean(box));
      }
      expect((await $.prompt.submit({ text: 'a real prompt' })).text).toBe('a real prompt');
      if (!submittedBeforeResult) expect(JSON.stringify(await ui.drawn())).toEqual(shown);
      await $.turn.complete({ answer: 'done' });
      const afterTurn = JSON.stringify(await ui.drawn());
      expect(afterTurn).not.toContain('unity gateway smart router');
      expect(afterTurn).not.toContain('Ctrl+O for Smart Router validation details');

      // A later session start neither polls again nor repeats the notice.
      const reads = state.reads;
      await $.session.start(start);
      await clock.advance(2000);
      expect(state.reads).toBe(reads);
      expect(state.opened).toBe(0);
      expect(state.notices.length).toBe(result.message ? 1 : 0);
      expect(await hint($)).toBe(routed ? 'smart router ON' : 'smart router OFF');
      await ui.unmount();
    });
  }
}

test('startup validation shows the band after 12 seconds without a result and stops polling', { options: ROUTED }, async ($, on) => {
  const { clock, state, hint } = engine(on);
  const ui = await $.ui.mount(abovePrompt);
  await $.session.start(start);
  await clock.advance(11900);
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

test('a result already written by session start shows the band without polling', { options: ROUTED }, async ($, on) => {
  const { clock, files, state, hint } = engine(on);
  files['/session/env.validation.json'] = '{"message":null}';
  const ui = await $.ui.mount(abovePrompt);
  expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
  await $.session.start(start);
  expect(JSON.stringify(await ui.drawn())).toContain('unity gateway smart router');
  expect(await hint($)).toBe('smart router ON');
  await clock.advance(1000);
  expect(state.reads).toBe(1);
  await ui.unmount();
});

test('a session without controls shows the band once session start runs', { options: ROUTED }, async ($, on) => {
  const { env, state } = engine(on);
  env.UCODE_SESSION_ENV_FILE = undefined;
  const ui = await $.ui.mount(abovePrompt);
  expect(JSON.stringify(await ui.drawn())).not.toContain('unity gateway smart router');
  await $.session.start(start);
  expect(JSON.stringify(await ui.drawn())).toContain('unity gateway smart router');
  expect(state.reads).toBe(0);
  await ui.unmount();
});
