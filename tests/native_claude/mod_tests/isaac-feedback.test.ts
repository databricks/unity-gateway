import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { createIsaacFeedbackState, registerIsaacFeedback } from '../hooks/isaac-feedback.ts';
import { register } from '../hooks/register.ts';

test('feedback opens before off finishes, and Cancel does not stop it', async () => {
  const handlers: Record<string, any> = {};
  const events: string[] = [];
  let finish!: () => void;
  const pending = new Promise<void>((resolve) => { finish = resolve; });
  const state = createIsaacFeedbackState();
  state.readRoutingEnabled = async () => true;
  state.disableRouting = async () => {
    events.push('off started');
    await pending;
    events.push('off finished');
  };
  registerIsaacFeedback(((event: string, _filter: unknown, handler: unknown) => {
    handlers[event] = handler;
  }) as On, state);
  const component = (props: any) => props;
  const $ = {
    ui: {
      open: async () => { events.push('form opened'); return { isPlaced: true }; },
      close: async () => { events.push('form closed'); },
      log: (text: string) => { events.push(text); },
      resolve: () => ({ Box: component, Text: component, Input: component, Button: component }),
    },
  };

  await handlers['command.run']($, { args: 'off' });
  expect(events).toEqual(['off started', 'form opened']);
  const form = handlers['ui.render']($, { requestId: 'smart-router-feedback', surface: 'terminal' });
  const cancel = form.children.find((child: any) => child.key === 'cancel');
  expect(cancel.label).toBe('Cancel');
  await cancel.onPress();
  finish();
  await pending;
  await Promise.resolve();
  expect(events).toEqual([
    'off started', 'form opened', 'form closed', 'off finished', 'Smart Router is off',
  ]);
});

test('the command entry point invokes feedback only when the flag is exactly 1', async () => {
  const commands: any[] = [];
  register(((event: string, _filter: unknown, handler: unknown) => {
    if (event === 'command.run') commands.push(handler);
  }) as On, { ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1', unset: [], recipe: 'task_v3' });
  for (const flag of [undefined, '', '0', 'true', 'yes', '2', ' 1', '1 ', '1']) {
    const env: Record<string, string | undefined> = {
      ISAAC_SMART_ROUTER_FEEDBACK: flag,
      UCODE_SMART_ROUTER_PYTHON: '/ug/python',
      UCODE_SESSION_ENV_FILE: '/session/env.json',
    };
    const $ = {
      env: { get: async (key: string) => env[key], set: async () => {} },
      process: { run: async () => ({ exitCode: 0 }) },
      ui: { invalidate: () => {} },
    };
    const next = () => ({ text: 'feedback invoked' });
    const reply = await commands[0]($, { args: 'off' }, next);
    expect(reply.text).toBe(flag === '1' ? 'feedback invoked' : 'Smart Router is off');
  }
});
