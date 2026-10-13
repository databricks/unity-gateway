import { expect, test } from 'claude-code/testing';

test('status is off when routing was not enabled at launch', async ($) => {
  expect(await $.command.run({command: 'smart-router', args: 'status'})).toEqual({
    text: 'Smart Router is off for this session.',
  });
});

test('status output renders ON and OFF badges on both surfaces', async ($) => {
  for (const surface of ['terminal', 'desktop'] as const) {
    for (const enabled of [true, false]) {
      const ui = await $.ui.mount({
        plugin: 'ug-smart-router',
        component: 'CommandOutput',
        surface,
        requestId: 'router-status',
        props: {
          command: 'smart-router',
          text: `Smart Router is ${enabled ? 'on' : 'off'} for this session.`,
        },
      });
      expect(await ui.find({type: 'Text', text: 'Unity Gateway Smart Router'})).toBeDefined();
      const badge = await ui.find({type: 'Text', text: enabled ? ' ON ' : ' OFF '});
      expect(badge.props).toMatchObject({
        bold: true, color: 'black', backgroundColor: enabled ? 'green' : 'gray',
      });
      await ui.unmount();
    }
  }
});

test('other command output and status errors retain the original rendering', async ($, on) => {
  on('ui.render', () => ({type: 'Text', props: {}, children: ['original output']}));
  for (const props of [
    {command: 'other', text: 'Smart Router is on for this session.'},
    {command: 'smart-router', text: 'Could not read Smart Router status.'},
  ]) {
    const ui = await $.ui.mount({
      plugin: 'ug-smart-router',
      component: 'CommandOutput', surface: 'terminal', requestId: 'other-output', props,
    });
    expect(await ui.find({type: 'Text', text: 'original output'})).toBeDefined();
    await ui.unmount();
  }
});

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
