import type { AgentInfo, On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';
import { RECIPE_FIELD } from '../hooks/constants.ts';
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
const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

// A pinned environment variable is the policy refusal the managed security default
// raises; only the extra body may carry the recipe.
function setup(on: On, { body = CALLER_BODY, pinned = '', launched = true } = {}) {
  // A routed launch puts its flags in the environment; session.start reads them back
  // so a reload keeps the toggle the session last applied.
  const env: Record<string, string | undefined> = {
    CLAUDE_CODE_EXTRA_BODY: body,
    ENABLE_SMART_ROUTING_V2: launched ? ROUTED.ENABLE_SMART_ROUTING_V2 : '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: launched ? ROUTED.ENABLE_SMART_ROUTING_SUBAGENT_ONLY : '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: launched ? ROUTED.ENABLE_SMART_ROUTER_ORCHESTRATOR : '0',
  };
  const writes: string[] = [];
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    writes.push(e.name);
    if (e.name === pinned) return { deny: `env (managed): ${e.name} is not for plugins outside policy to change` };
    env[e.name] = e.value;
    return { value: undefined };
  });
  return { env, writes };
}

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

test('a denied extra-body write restores the environment and reports failure', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on, { pinned: 'CLAUDE_CODE_EXTRA_BODY' });

  const reply = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(reply.text).toContain('Could not update Smart Router');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);
  // The rollback puts the flags the launch set back, so routing stays as it was.
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

test('a caller body that is not a JSON object is left alone', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on, { body: 'not json' });

  const reply = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(reply.text).toContain('Could not update Smart Router');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe('not json');
});

test('a launch without routing keeps the command inert', { options: { ...ROUTED, ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0' } }, async ($, on) => {
  const { env } = setup(on, { launched: false });
  on('session.start', () => ({ cwd: '/work' }));
  await $.session.start(start);

  const reply = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(reply.text).toContain('was not enabled');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);
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

// The engine's own process, drawing, prompt, and turn answers sit beneath the mod, and
// session controls are available so toggles succeed.
function engine(on: On, env: Record<string, string | undefined>, agents: AgentInfo[] = []) {
  const clock = mock.clock(on);
  env.UCODE_SMART_ROUTER_PYTHON = '/ug/python';
  env.UCODE_SESSION_ENV_FILE = '/session/env.json';
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }));
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['theirs'] }));
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: '' }));
  on('agent.list', () => ({ value: agents }));
  // The engine draws the prompt hint row from the tail the mod passes down.
  const tails: unknown[] = [];
  on('ui.render', { component: 'PromptHint' }, ($, e) => {
    tails.push(e.props.tail);
    return $.ui.resolve(e).Text({ children: ['hint'] });
  });
  return { tails, clock };
}

// The tail the mod hands the engine for the prompt hint row under the input.
async function hintTail($: any, tails: unknown[]): Promise<unknown> {
  tails.length = 0;
  await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: '' } });
  return tails.at(-1);
}

test('the band hides after a turn and returns after a toggle while the hint row tracks the state', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const { tails } = engine(on, env);
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
  const { env } = setup(on);
  const { tails } = engine(on, env);
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
  const { env } = setup(on);
  const { tails } = engine(on, env);
  await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: 'theirs' } });
  expect(tails.at(-1)).toBe('theirs · smart router ON');
});

test('the hint row counts running subagents across parent turns and routing toggles', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents: AgentInfo[] = ['pending', 'waiting', 'idle', 'completed', 'failed', 'killed'].map((status) => ({
    id: status, description: status, type: 'general-purpose', status,
  }));
  const { tails, clock } = engine(on, env, agents);
  const row = await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: 'theirs' } });
  const hint = async () => {
    await clock.advance(1000);
    await row.drawn();
    return tails.at(-1);
  };
  expect(await hint()).toBe('theirs · smart router ON');
  const renders = tails.length;
  await hint();
  expect(tails.length).toBe(renders);

  agents.push({ id: 'a', description: 'First', type: 'Explore', status: 'running' });
  expect(await hint()).toBe('theirs · smart router ON · 1 subagent running');
  agents.push({ id: 'b', description: 'Second', type: 'general-purpose', status: 'running' });
  expect(await hint()).toBe('theirs · smart router ON · 2 subagents running');

  await $.prompt.submit({ text: 'Wait for the background agents.' });
  await $.turn.complete({ answer: 'Waiting.' });
  expect(await hint()).toBe('theirs · smart router ON · 2 subagents running');
  await $.command.run({ command: 'smart-router', args: 'off' });
  expect(await hint()).toBe('theirs · smart router OFF · 2 subagents running');

  agents[agents.length - 1].status = 'completed';
  expect(await hint()).toBe('theirs · smart router OFF · 1 subagent running');
  agents[agents.length - 2].status = 'killed';
  expect(await hint()).toBe('theirs · smart router OFF');
  await row.unmount();
});

test('the band is gone by the second turn after either toggle', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const { tails } = engine(on, env);
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
