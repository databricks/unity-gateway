import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
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

test('the band hides after the first turn and returns for the turn after a toggle', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  env.UCODE_SMART_ROUTER_PYTHON = '/ug/python';
  env.UCODE_SESSION_ENV_FILE = '/session/env.json';
  // The engine's own process, drawing, prompt, and turn answers sit beneath the mod.
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }));
  on('ui.render', ($, e) => $.ui.resolve(e).Text({ children: ['theirs'] }));
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: '' }));
  const band = await $.ui.mount(ABOVE_PROMPT);
  expect(hasBand(await band.drawn())).toBe(true);

  // The band stays through the prompt it first appears with.
  await $.prompt.submit({ text: 'first' });
  expect(hasBand(await band.drawn())).toBe(true);
  await $.turn.complete({ answer: 'done' });
  const hidden = await band.drawn();
  expect(hasBand(hidden)).toBe(false);
  expect(JSON.stringify(hidden)).toContain('theirs');

  expect((await $.command.run({ command: 'smart-router', args: 'off' })).text).toBe('Smart Router is off');
  expect(hasBand(await band.drawn())).toBe(true);
  await $.prompt.submit({ text: 'second' });
  await $.turn.complete({ answer: 'done' });
  expect(hasBand(await band.drawn())).toBe(false);
});
