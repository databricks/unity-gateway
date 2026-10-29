import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { headerLines } from '../hooks/register.ts';

const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

function setup(on: On, env: Record<string, string | undefined>) {
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    env[e.name] = e.value;
    return { value: undefined };
  });
  // Stands in for Claude's own handling beneath the mod.
  on('session.start', ($, e) => ({ cwd: e.cwd }));
  return env;
}

test('session start adds the launch headers after the managed ones', async ($, on) => {
  const env = setup(on, {
    ANTHROPIC_CUSTOM_HEADERS: 'x-databricks-use-coding-agent-mode: true',
    UG_HEADERS: 'x-databricks-traffic-id: testenv://liteswap/pod-1',
  });

  await $.session.start(start);

  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe(
    'x-databricks-use-coding-agent-mode: true\nx-databricks-traffic-id: testenv://liteswap/pod-1',
  );
});

test('a header already sent keeps its value, and reapplying changes nothing', () => {
  const base = 'X-Team: eng\nUser-Agent: ucode/1';
  const merged = headerLines(base, 'x-team: other\nX-New: 1');
  expect(merged).toBe('X-Team: eng\nUser-Agent: ucode/1\nX-New: 1');
  expect(headerLines(merged, 'x-team: other\nX-New: 1')).toBeUndefined();
});

test('malformed and repeated launch lines are skipped, and CRLF bases are read', () => {
  const extra = 'no colon\nbad name: 1\nX-Empty:  \nX-Ctl: a\u0001b\nX-New: 1\nx-new: 2';
  expect(headerLines('X-Team: eng\r\nUser-Agent: ucode/1', extra)).toBe(
    'X-Team: eng\nUser-Agent: ucode/1\nX-New: 1',
  );
  expect(headerLines('X-Team: eng', 'no colon\nbad name: 1')).toBeUndefined();
});

test('without launch headers the variable is left alone', async ($, on) => {
  const env = setup(on, { ANTHROPIC_CUSTOM_HEADERS: 'X-Team: eng' });

  await $.session.start(start);

  expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe('X-Team: eng');
  expect(headerLines(undefined, '\n')).toBeUndefined();
});
