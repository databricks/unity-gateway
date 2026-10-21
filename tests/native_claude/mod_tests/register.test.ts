import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { DISABLED_RECIPE, RECIPE_FIELD } from '../hooks/constants.ts';
import { mergeExtraBody } from '../hooks/extra-body.ts';

const RECIPE = 'task_v3';
const CALLER_BODY = '{"caller_field":"keep"}';
const ROUTED = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
  unset: [],
  recipe: RECIPE,
};
const SESSION_FILE = '/session/env.json';
const WHEN_ON = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
};
const STARTED_ON = { enabled: true, when_on: WHEN_ON, ever_enabled: true };
const STARTED_OFF = { enabled: false, when_on: WHEN_ON, ever_enabled: false };
const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

// A pinned environment variable is the policy refusal the managed security default
// raises; only the extra body may carry the recipe. UG's CLI owns the session file, so the
// fake CLI flips the switch the mod reads back.
function setup(
  on: On,
  { body = CALLER_BODY, pinned = '', session = STARTED_ON, cliExitCode = 0, feedback = '' } = {},
) {
  const env: Record<string, string | undefined> = {
    CLAUDE_CODE_EXTRA_BODY: body,
    UCODE_SMART_ROUTER_PYTHON: '/ug/python',
    UCODE_SESSION_ENV_FILE: SESSION_FILE,
    ISAAC_SMART_ROUTER_FEEDBACK: feedback,
  };
  const controls = { ...session };
  const writes: string[] = [];
  const runs: string[][] = [];
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    writes.push(e.name);
    if (e.name === pinned) return { deny: `env (managed): ${e.name} is not for plugins outside policy to change` };
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('fs.read', ($, e) => e.path === SESSION_FILE ? { value: JSON.stringify(controls) } : { deny: 'missing' });
  on('process.run', ($, e) => {
    runs.push(e.argv);
    if (cliExitCode === 0) {
      const enabled = e.argv.includes('--enable-smart-routing');
      Object.assign(controls, { enabled, ever_enabled: controls.ever_enabled || enabled });
    }
    return { value: { exitCode: cliExitCode, stdout: '', stderr: cliExitCode ? 'failed' : '' } };
  });
  return { env, writes, controls, runs };
}

const recipeOf = (env: Record<string, string | undefined>) => JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)[RECIPE_FIELD];

test('session start carries the launch recipe and forwards the event and result', { options: ROUTED }, async ($, on) => {
  const { env, writes } = setup(on);
  let calls = 0;
  on('session.start', ($, e) => {
    calls += 1;
    expect(e).toMatchObject(start);
    return { cwd: '/resolved-work' };
  });

  const result = await $.session.start(start);
  expect(calls).toBe(1);
  expect(result).toEqual({ cwd: '/resolved-work' });
  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({ caller_field: 'keep', [RECIPE_FIELD]: RECIPE });
  // The recipe left ANTHROPIC_CUSTOM_HEADERS, which a managed config pins.
  expect(writes).not.toContain('ANTHROPIC_CUSTOM_HEADERS');
});

test('a denied extra-body write keeps the switch and reports failure', { options: ROUTED }, async ($, on) => {
  const { env, controls } = setup(on, { pinned: 'CLAUDE_CODE_EXTRA_BODY' });

  const reply = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(reply.text).toContain('Could not update Smart Router');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);
  expect(controls.enabled).toBe(true);
});

test('a failed switch restores the recipe and reports failure', { options: ROUTED }, async ($, on) => {
  const { env, controls } = setup(on, { cliExitCode: 1 });

  const reply = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(reply.text).toContain('Could not update Smart Router');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);
  expect(controls.enabled).toBe(true);
});

test('a caller body that is not a JSON object is left alone', { options: ROUTED }, async ($, on) => {
  const { env, controls } = setup(on, { body: 'not json' });

  const reply = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(reply.text).toContain('Could not update Smart Router');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe('not json');
  expect(controls.enabled).toBe(true);
});

test('a session that starts off adds no recipe until the first on', { options: ROUTED }, async ($, on) => {
  const { env, writes, controls } = setup(on, { session: STARTED_OFF });
  on('session.start', () => ({ cwd: '/work' }));
  await $.session.start(start);
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);

  expect((await $.command.run({ command: 'smart-router', args: 'off' })).text).toBe('Smart Router is off');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);

  expect((await $.command.run({ command: 'smart-router', args: 'on' })).text).toBe('Smart Router is on');
  expect(recipeOf(env)).toBe(RECIPE);
  expect(controls).toMatchObject({ enabled: true, ever_enabled: true });

  await $.command.run({ command: 'smart-router', args: 'off' });
  expect(recipeOf(env)).toBe(DISABLED_RECIPE);
  // Hooks read the session file, so toggles leave the routing variables alone.
  expect(new Set(writes)).toEqual(new Set(['CLAUDE_CODE_EXTRA_BODY']));
});

test('Isaac asks no off reason while the session switch is off', { options: ROUTED }, async ($, on) => {
  const { runs } = setup(on, { session: STARTED_OFF, feedback: '1' });

  expect((await $.command.run({ command: 'smart-router', args: 'off' })).text).toBe('Smart Router is off');
  expect(runs).toEqual([]);
});

test('merging keeps other request metadata and rejects a non-object body', async () => {
  expect(JSON.parse(mergeExtraBody('{"caller":1,"label":"old"}', { label: 'new', nested: { enabled: true } })))
    .toEqual({ caller: 1, label: 'new', nested: { enabled: true } });
  expect(JSON.parse(mergeExtraBody(undefined, { value: 42 }))).toEqual({ value: 42 });
  for (const raw of ['null', '[]', 'true', '1', '"text"', 'broken']) {
    expect(() => mergeExtraBody(raw, {})).toThrow();
  }
});

const ABOVE_PROMPT = { plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} } as const;

const hasBand = (tree: unknown): boolean => JSON.stringify(tree).includes('unity gateway smart router');

// The engine's own drawing, prompt, and turn answers sit beneath the mod.
function engine(on: On) {
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['theirs'] }));
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: '' }));
  // The engine draws the prompt hint row from the tail the mod passes down.
  const tails: unknown[] = [];
  on('ui.render', { component: 'PromptHint' }, ($, e) => {
    tails.push(e.props.tail);
    return $.ui.resolve(e).Text({ children: ['hint'] });
  });
  return { tails };
}

// The tail the mod hands the engine for the prompt hint row under the input.
async function hintTail($: any, tails: unknown[]): Promise<unknown> {
  tails.length = 0;
  await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: '' } });
  return tails.at(-1);
}

test('the band hides after a turn and returns after a toggle while the hint row tracks the state', { options: ROUTED }, async ($, on) => {
  setup(on);
  const { tails } = engine(on);
  const hint = () => hintTail($, tails);
  const band = await $.ui.mount(ABOVE_PROMPT);
  expect(hasBand(await band.drawn())).toBe(true);
  expect(await hint()).toBe('smart router ON');

  // The band stays through the prompt it first appears with.
  await $.prompt.submit({ text: 'first' });
  expect(hasBand(await band.drawn())).toBe(true);
  await $.turn.complete({ answer: 'done' });
  const hidden = await band.drawn();
  expect(hasBand(hidden)).toBe(false);
  expect(JSON.stringify(hidden)).toContain('theirs');
  // The hint row keeps the state once the band hides.
  expect(await hint()).toBe('smart router ON');

  expect((await $.command.run({ command: 'smart-router', args: 'off' })).text).toBe('Smart Router is off');
  expect(hasBand(await band.drawn())).toBe(true);
  // The hint row flips with the toggle, not after the next turn.
  expect(await hint()).toBe('smart router OFF');
  await $.prompt.submit({ text: 'second' });
  await $.turn.complete({ answer: 'done' });
  expect(hasBand(await band.drawn())).toBe(false);
  expect(await hint()).toBe('smart router OFF');
});

test('the hint row reads ON or OFF from each toggle through the second turn', { options: ROUTED }, async ($, on) => {
  setup(on);
  const { tails } = engine(on);
  const hint = () => hintTail($, tails);
  expect(await hint()).toBe('smart router ON');

  for (const [action, state] of [['off', 'OFF'], ['on', 'ON'], ['off', 'OFF'], ['on', 'ON']] as const) {
    await $.command.run({ command: 'smart-router', args: action });
    const expected = `smart router ${state}`;
    expect(await hint()).toBe(expected);
    for (const text of ['first', 'second']) {
      await $.prompt.submit({ text: `${text} after ${action}` });
      expect(await hint()).toBe(expected);
      await $.turn.complete({ answer: 'done' });
      expect(await hint()).toBe(expected);
    }
  }
});

test('the hint row keeps a tail another hook already set', { options: ROUTED }, async ($, on) => {
  setup(on);
  const { tails } = engine(on);
  await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: 'theirs' } });
  expect(tails.at(-1)).toBe('theirs · smart router ON');
});

test('the band is gone by the second turn after either toggle', { options: ROUTED }, async ($, on) => {
  setup(on);
  const { tails } = engine(on);
  const band = await $.ui.mount(ABOVE_PROMPT);
  const turn = async (text: string) => {
    await $.prompt.submit({ text });
    await $.turn.complete({ answer: 'done' });
  };
  await turn('launch turn');
  expect(hasBand(await band.drawn())).toBe(false);

  for (const [action, pill] of [['off', ' OFF '], ['on', ' ON ']] as const) {
    expect((await $.command.run({ command: 'smart-router', args: action })).text).toBe(`Smart Router is ${action}`);
    const shown = await band.drawn();
    expect(hasBand(shown)).toBe(true);
    expect(JSON.stringify(shown)).toContain(pill);

    // First turn after the toggle: the band shows while it runs, then hides.
    await $.prompt.submit({ text: `first after ${action}` });
    expect(hasBand(await band.drawn())).toBe(true);
    await $.turn.complete({ answer: 'done' });
    expect(hasBand(await band.drawn())).toBe(false);

    // Second turn: the band stays gone throughout.
    await $.prompt.submit({ text: `second after ${action}` });
    expect(hasBand(await band.drawn())).toBe(false);
    await $.turn.complete({ answer: 'done' });
    expect(hasBand(await band.drawn())).toBe(false);
  }
});

test('a session that starts off shows no band or hint until the first on', { options: ROUTED }, async ($, on) => {
  setup(on, { session: STARTED_OFF });
  const { tails } = engine(on);
  const band = await $.ui.mount(ABOVE_PROMPT);
  expect(hasBand(await band.drawn())).toBe(false);
  expect(await hintTail($, tails)).toBe('');
  await $.prompt.submit({ text: 'first' });
  await $.turn.complete({ answer: 'done' });
  expect(hasBand(await band.drawn())).toBe(false);

  await $.command.run({ command: 'smart-router', args: 'on' });
  expect(hasBand(await band.drawn())).toBe(true);
  expect(await hintTail($, tails)).toBe('smart router ON');
  await $.command.run({ command: 'smart-router', args: 'off' });
  expect(JSON.stringify(await band.drawn())).toContain(' OFF ');
  expect(await hintTail($, tails)).toBe('smart router OFF');
});
