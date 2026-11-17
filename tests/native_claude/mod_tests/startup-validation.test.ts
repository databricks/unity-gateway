import { expect, mock, test } from 'claude-code/testing';

const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;
const abovePrompt = {
  plugin: 'ug-smart-router', component: 'AbovePrompt', surface: 'terminal', props: {},
} as const;

for (const message of [
  null,
  'Smart Router does not support this model menu. Available options: claude-sonnet (claude_code), glm-5 (claude_code). Routing remains enabled.',
  'Smart Router compatibility check unavailable. Routing remains enabled.',
]) {
  for (const submittedBeforeResult of [false, true]) {
    test(`startup validation displays ${message ?? 'support'} once with earlier prompt ${submittedBeforeResult}`, async ($, on) => {
      const clock = mock.clock(on);
      let result: string | undefined;
      let reads = 0;
      let opened = 0;
      on('env.get', ($, e) => ({ value: e.name === 'UCODE_SESSION_ENV_FILE' ? '/session/env.json' :
        e.name === 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY' ? '1' : undefined }));
      on('session.start', () => ({ cwd: '/resolved-work' }));
      on('fs.read', ($, e) => {
        if (e.path !== '/session/env.validation.json') return { value: '{}' };
        reads += 1;
        if (result === undefined) throw new Error('pending');
        return { value: result };
      });
      on('ui.open', () => { opened++; return { value: { isPlaced: true } }; });
      on('ui.toast', () => { opened++; return { value: undefined }; });
      on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['host content'] }));
      on('prompt.submit', ($, e) => ({ text: e.text }));
      on('turn.complete', () => ({ text: '' }));
      on('agent.list', () => ({ value: [] }));

      expect(await $.session.start(start)).toEqual({ cwd: '/resolved-work' });
      const ui = await $.ui.mount(abovePrompt);
      const initial = JSON.stringify(await ui.drawn());
      expect(initial).not.toContain('Smart Router validation');
      await clock.advance(1000);
      expect(reads).toBe(1);
      if (submittedBeforeResult) {
        expect((await $.prompt.submit({ text: 'before validation' })).text).toBe('before validation');
      }
      result = JSON.stringify({ message });
      await clock.advance(1000);
      const shown = JSON.stringify(await ui.drawn());
      expect(shown).toContain('host content');
      expect(shown).toContain(' ON ');
      if (message) {
        expect(shown).toContain('Smart Router validation');
        expect(shown).toContain(message);
      } else {
        expect(shown).not.toContain('Smart Router validation');
      }
      if (submittedBeforeResult) {
        await $.turn.complete({ answer: 'earlier turn done' });
        const afterEarlierTurn = JSON.stringify(await ui.drawn());
        expect(afterEarlierTurn).toContain('host content');
        expect(afterEarlierTurn).not.toContain('unity gateway smart router');
        if (message) expect(afterEarlierTurn).toContain(message);
        else expect(afterEarlierTurn).not.toContain('Smart Router validation');
      }
      expect((await $.prompt.submit({ text: 'a real prompt' })).text).toBe('a real prompt');
      if (!submittedBeforeResult) expect(JSON.stringify(await ui.drawn())).toEqual(shown);
      await $.turn.complete({ answer: 'done' });
      expect(JSON.stringify(await ui.drawn())).not.toContain('Smart Router validation');
      await $.session.start(start);
      await clock.advance(2000);
      expect(reads).toBe(2);
      expect(opened).toBe(0);
      expect(JSON.stringify(await ui.drawn())).toContain('host content');
      expect(JSON.stringify(await ui.drawn())).not.toContain('Smart Router validation');
      await ui.unmount();
    });
  }
}
