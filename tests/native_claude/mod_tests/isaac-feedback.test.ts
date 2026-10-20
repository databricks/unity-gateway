import type { On } from 'claude-code';
import { expect, test } from 'claude-code/testing';
import { createIsaacFeedbackState, registerIsaacFeedback } from '../hooks/isaac-feedback.ts';

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
    env: { get: async () => '1' },
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
