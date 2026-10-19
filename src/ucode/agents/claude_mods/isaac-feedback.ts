import type { On } from 'claude-code';

const FEEDBACK_PANE = 'smart-router-feedback';

type FeedbackState = {
  submitted: boolean;
  reason: string;
  readRoutingEnabled?: () => Promise<boolean>;
  disableRouting?: (sessionControlArgs: readonly string[]) => Promise<void>;
};

export function createIsaacFeedbackState(): FeedbackState {
  return { submitted: false, reason: '' };
}

export function registerIsaacFeedback(on: On, state: FeedbackState): void {
  on('command.run', {command: 'smart-router'}, async ($, e, next) => {
    if (e.args.trim() !== 'off' || await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK') !== '1') {
      return next(e);
    }
    try {
      if (!state.readRoutingEnabled || !state.disableRouting) {
        throw new Error('Smart Router controls are unavailable.');
      }
      if (!await state.readRoutingEnabled()) return {text: 'Smart Router is off'};
      if ((await $.command.list()).some((command) => command.name === 'isaac-feedback')) {
        state.submitted = false;
        state.reason = '';
        const pane = await $.ui.open({
          id: FEEDBACK_PANE, title: 'Turn Smart Router off', focus: true,
          closeOnEscape: true, rows: 6, columns: 56,
        });
        if (pane.isPlaced) return {text: 'A reason of at least 16 characters is required to turn Smart Router off.'};
        // Do not leave a delayed question waiting for an unrelated task.
        await $.ui.close({ id: FEEDBACK_PANE });
      }
    } catch {
      // No reason was collected, so leave routing unchanged.
    }
    return {text: 'Could not open the feedback form. Smart Router is still on; retry /smart-router off in an Isaac session with /isaac-feedback available.'};
  });

  on('ui.render', { component: 'Pane' }, ($, e, next) => {
    if (e.requestId !== FEEDBACK_PANE || e.surface === 'mobile') return next(e);
    const { Box, Text, Input, Button } = $.ui.resolve(e);
    return Box({
      flexDirection: 'column',
      children: [
        Text({ children: ['Why are you turning Smart Router off?'] }),
        Text({ children: ['Enter at least 16 characters. Your reason and session evidence go to /isaac-feedback.'] }),
        Input({
          key: 'reason',
          label: 'Reason',
          value: state.reason,
          autoFocus: true,
          submitLabel: 'Send feedback and turn off',
          onInput: (value) => { state.reason = value; },
          onSubmit: async (value) => {
            if (state.submitted || !state.disableRouting) return;
            if (Array.from(value.trim()).length < 16) {
              $.ui.toast('Enter at least 16 characters, excluding surrounding whitespace.');
              return;
            }
            state.submitted = true;
            try {
              await state.disableRouting(['--smart-router-feedback', value]);
            } catch {
              state.submitted = false;
              $.ui.toast('Could not update Smart Router. Smart Router is still on; retry or restart through UG.');
              return;
            }
            try {
              await $.ui.close({ id: FEEDBACK_PANE });
              await $.command.run({ command: 'isaac-feedback', args: `--category smart-routing ${value}` });
            } catch {
              $.ui.toast('Smart Router is off, but feedback could not open. Retry with /isaac-feedback --category smart-routing.');
            }
          },
        }),
        Button({ key: 'cancel', label: 'Cancel (keep routing on)', onPress: () => $.ui.close({ id: FEEDBACK_PANE }) }),
      ],
    });
  });
}
