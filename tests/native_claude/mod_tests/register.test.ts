import { expect, test } from 'claude-code/testing';

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
