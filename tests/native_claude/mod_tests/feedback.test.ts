import type { EngineInterface, On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { register } from '../hooks/register.ts';

const ROUTED = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
  unset: [],
  recipe: 'task_v3',
};

// Claude 2.1.290's test host omits process.run, command.list and ui.open.
// Replace those engine boundaries, then drive the real registered hooks.
function fixture({ evidence = true, available = true, fail = '' } = {}) {
  let toggle: any;
  let render: any;
  const on = ((event: string, filter: any, handler: any) => {
    if (event === 'command.run') toggle = handler;
    if (event === 'ui.render' && filter.component === 'Pane') render = handler;
  }) as On;
  register(on, ROUTED);
  const env: Record<string, string | undefined> = {
    ...ROUTED,
    unset: undefined,
    CLAUDE_CODE_EXTRA_BODY: '{"caller":"keep"}',
    UCODE_SMART_ROUTER_PYTHON: '/launch/python',
    UCODE_SESSION_ENV_FILE: '/session/env.json',
    ISAAC_SMART_ROUTER_FEEDBACK: evidence ? '1' : '0',
  };
  let controls = 'on';
  const opened: any[] = [];
  const sent: any[] = [];
  const closed: any[] = [];
  const notices: string[] = [];
  const invalidated: string[] = [];
  const element = (type: string) => (props: any) => ({ type, ...props });
  const host = {
    env: {
      get: async (key: string) => env[key],
      set: async (key: string, value: string | undefined) => { env[key] = value; },
    },
    process: { run: async (argv: string[]) => {
      if (fail === 'controls') return { exitCode: 1, stdout: '', stderr: 'denied' };
      controls = argv.includes('--disable-smart-routing') ? 'off' : 'on';
      if (controls === 'off' && evidence) {
        expect(Array.from(argv[argv.indexOf('--smart-router-feedback') + 1].trim()).length).toBeGreaterThan(15);
      }
      return { exitCode: 0, stdout: '', stderr: '' };
    } },
    command: {
      list: async () => available ? [{ name: 'isaac-feedback' }] : [],
      run: async (input: any) => {
        if (fail === 'submission') throw new Error('command unavailable');
        sent.push(input);
        return {};
      },
    },
    ui: {
      invalidate: (event: string) => { invalidated.push(event); },
      open: async (input: any) => {
        expect(controls).toBe('on');
        expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
        if (fail === 'prompt') throw new Error('no UI');
        opened.push(input);
        return { isPlaced: true };
      },
      close: async (input: any) => { closed.push(input); },
      toast: (message: string) => { notices.push(message); },
      resolve: () => Object.fromEntries(['Box', 'Text', 'Input', 'Button'].map((name) => [name, element(name)])),
    },
  } as unknown as EngineInterface;
  return {
    env, opened, sent, closed, notices, invalidated,
    toggle: (args = 'off') => toggle(host, { args }),
    pane: () => render(host, { requestId: 'smart-router-feedback', surface: 'terminal' }),
  };
}

test('a valid reason is required before disabling, and its exact text is submitted once', async () => {
  const f = fixture();
  expect((await f.toggle()).text).toContain('at least 16 characters');
  expect(f.invalidated).toHaveLength(0);
  expect(f.opened).toHaveLength(1);
  expect(f.opened[0].closeOnEscape).toBe(true);
  expect(f.sent).toHaveLength(0);
  expect(f.env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('1');

  const input = f.pane().children.find((child: any) => child.key === 'reason');
  for (const value of ['  ', '123456789012345', ' 123456789012345 ', '😀'.repeat(15)]) {
    await input.onSubmit(value);
    expect(f.sent).toHaveLength(0);
    expect(f.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    expect(f.notices.at(-1)).toContain('at least 16 characters');
  }
  const reason = "  retry later: it's slow; $(this is text)\nand lost context  ";
  await input.onSubmit(reason);
  await input.onSubmit(reason);
  expect(f.sent).toEqual([{ command: 'isaac-feedback', args: `--category smart-routing ${reason}` }]);
  expect(f.closed).toEqual([{ id: 'smart-router-feedback' }]);
  expect(f.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(f.env.ENABLE_SMART_ROUTER_ORCHESTRATOR).toBe('0');
  expect(f.env.ISAAC_SMART_ROUTER_FEEDBACK).toBe('1');
  expect(JSON.parse(f.env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({ caller: 'keep', smart_router_recipe_name: 'DISABLED' });
  expect(f.invalidated).toEqual(['ui.render']);
});

test('Cancel keeps routing on; every subsequent on-to-off change requires a new reason', async () => {
  const f = fixture();
  await f.toggle();
  await f.pane().children.find((child: any) => child.key === 'cancel').onPress();
  expect(f.sent).toHaveLength(0);
  expect(f.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  await f.toggle();
  await f.pane().children.find((child: any) => child.key === 'reason').onSubmit(' 1234567890123456 ');
  expect(f.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(await f.toggle()).toEqual({ text: 'Smart Router is off' });
  expect(f.opened).toHaveLength(2);
  await f.toggle('on');
  await f.toggle();
  expect(f.opened).toHaveLength(3);
  expect(f.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(f.pane().children.find((child: any) => child.key === 'reason').value).toBe('');
  await f.pane().children.find((child: any) => child.key === 'reason').onSubmit('😀'.repeat(16));
  expect(f.sent).toHaveLength(2);
  expect(f.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
});

test('disabled rollout preserves normal toggles; unavailable feedback keeps routing on', async () => {
  const unenrolled = fixture({ evidence: false });
  expect(await unenrolled.toggle()).toEqual({ text: 'Smart Router is off' });
  expect(unenrolled.opened).toHaveLength(0);
  for (const options of [{ available: false }, { fail: 'prompt' }]) {
    const f = fixture(options);
    expect((await f.toggle()).text).toContain('Smart Router is still on');
    expect(f.opened).toHaveLength(0);
    expect(f.sent).toHaveLength(0);
    expect(f.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  }
});

test('submission failures leave routing off after a valid reason; failed toggles preserve routing', async () => {
  const f = fixture({ fail: 'submission' });
  await f.toggle();
  await f.pane().children.find((child: any) => child.key === 'reason').onSubmit('The task was too slow');
  expect(f.notices[0]).toContain('Smart Router is off');
  expect(f.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(f.sent).toHaveLength(0);

  const failedToggle = fixture({ fail: 'controls' });
  await failedToggle.toggle();
  await failedToggle.pane().children.find((child: any) => child.key === 'reason').onSubmit('The task was too slow');
  expect(failedToggle.notices[0]).toContain('Smart Router is still on');
  expect(failedToggle.sent).toHaveLength(0);
  expect(failedToggle.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});
