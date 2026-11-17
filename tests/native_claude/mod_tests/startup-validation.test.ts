import { expect, mock, test } from 'claude-code/testing';

const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

for (const message of [null, 'Smart Router does not support this model menu.']) {
  test(`startup validation reports ${message ?? 'support'} once after startup`, async ($, on) => {
    const clock = mock.clock(on);
    let result: string | undefined;
    let reads = 0;
    const notices: string[] = [];
    on('env.get', ($, e) => ({ value: e.name === 'UCODE_SESSION_ENV_FILE' ? '/session/env.json' : undefined }));
    on('session.start', () => ({ cwd: '/resolved-work' }));
    on('fs.read', ($, e) => {
      if (e.path !== '/session/env.validation.json') return { value: '{}' };
      reads += 1;
      if (result === undefined) throw new Error('pending');
      return { value: result };
    });
    on('ui.toast', ($, e) => {
      notices.push(e.text);
      return { value: undefined };
    });

    expect(await $.session.start(start)).toEqual({ cwd: '/resolved-work' });
    expect(notices).toEqual([]);
    await clock.advance(1000);
    expect(reads).toBe(1);
    result = JSON.stringify({ message });
    await clock.advance(1000);
    expect(notices).toEqual(message ? [message] : []);
    await $.session.start(start);
    await clock.advance(2000);
    expect(reads).toBe(2);
    expect(notices).toEqual(message ? [message] : []);
  });
}
