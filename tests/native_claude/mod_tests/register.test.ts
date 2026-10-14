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
    UCODE_SMART_ROUTER_PYTHON: '/launch/python',
    UCODE_SESSION_ENV_FILE: '/session/env.json',
  };
  const writes: string[] = [];
  const argv: string[][] = [];
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    writes.push(e.name);
    if (e.name === pinned) return { deny: `env (managed): ${e.name} is not for plugins outside policy to change` };
    env[e.name] = e.value;
    return { value: undefined };
  });
  on('process.run', ($, e) => {
    argv.push([...e.argv]);
    return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } };
  });
  on('session.start', () => ({ cwd: '/work' }));
  return { env, writes, argv };
}

test('session start forwards the event and result', async ($, on) => {
  const event = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;
  let calls = 0;
  on('session.start', ($, e) => {
    calls += 1;
    expect(e).toMatchObject(event);
    return { cwd: '/resolved-work' };
  });

  const result = await $.session.start(event);
  expect(calls).toBe(1);
  expect(result).toEqual({ cwd: '/resolved-work' });
});

test('session start carries the launch recipe in the extra body', { options: ROUTED }, async ($, on) => {
  const { env, writes } = setup(on);

  await $.session.start(start);

  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({ caller_field: 'keep', [RECIPE_FIELD]: RECIPE });
  // The recipe left ANTHROPIC_CUSTOM_HEADERS, which a managed config pins.
  expect(writes).not.toContain('ANTHROPIC_CUSTOM_HEADERS');
});

test('the toggle rewrites the recipe and the routing flags', { options: ROUTED }, async ($, on) => {
  const { env, argv } = setup(on);
  await $.session.start(start);

  const off = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(off.text).toContain('Smart Router is off');
  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({ caller_field: 'keep', [RECIPE_FIELD]: DISABLED_RECIPE });
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('0');
  expect(argv).toEqual([['/launch/python', '-m', 'ucode.cli', 'claude', '--disable-smart-routing']]);

  const on_ = await $.command.run({ command: 'smart-router', args: 'on' });

  expect(on_.text).toContain('Smart Router is on');
  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({ caller_field: 'keep', [RECIPE_FIELD]: RECIPE });
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(argv[1]).toEqual(['/launch/python', '-m', 'ucode.cli', 'claude', '--enable-smart-routing']);
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
