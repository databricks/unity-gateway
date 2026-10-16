import type { On } from 'claude-code';

export const FEEDBACK_PANE = 'smart-router-feedback';

export type FeedbackState = {
  submitted: boolean;
  reason: string;
  disableRouting?: (reason: string) => Promise<void>;
};

export function registerFeedback(on: On, state: FeedbackState): void {
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
              await state.disableRouting(value);
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
